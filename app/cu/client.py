"""Async REST client for Azure AI Content Understanding."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any
from urllib.parse import quote, quote_plus

import httpx

logger = logging.getLogger(__name__)

TERMINAL_STATES = {"Succeeded", "Failed", "Canceled"}


def safe_error_detail(error: Exception, *, secrets: tuple[str, ...] = ()) -> str:
    message = html.unescape(str(error))
    for secret in sorted(filter(None, secrets), key=len, reverse=True):
        for value in {secret, quote(secret, safe=""), quote_plus(secret)}:
            message = message.replace(value, "[redacted]")
    message = re.sub(r"https?(?:://|%3a%2f%2f)[^\s<>\"']+", "[redacted URL]", message, flags=re.IGNORECASE)
    message = re.sub(
        r"(?i)\b(sig|signature|token|access_token|api[-_]?key|accountkey|authorization|ocp-apim-subscription-key)\b\s*[:=]\s*[^\s,;<>]+",
        r"\1=[redacted]", message,
    )
    message = re.sub(r"[\x00-\x1f\x7f]+", " ", message)
    return message[:2000]


def _error_message(error: Any, fallback: str) -> str:
    if not isinstance(error, dict):
        return fallback
    parts: list[str] = []
    code = error.get("code")
    message = error.get("message")
    if code or message:
        detail = f"{code}: {message}" if code and message else str(message or code)
        if error.get("target"):
            detail += f" (target: {error['target']})"
        parts.append(detail)
    nested = [error.get("innererror"), *(error.get("details") or [])]
    for item in nested:
        detail = _error_message(item, "")
        if detail and detail not in parts:
            parts.append(detail)
    return "; ".join(parts) or fallback


class ContentUnderstandingError(RuntimeError):
    def __init__(self, message: str, status_code: int | None = None, payload: Any = None):
        super().__init__(message)
        self.status_code = status_code
        self.payload = payload


class ContentUnderstandingClient:
    """Thin wrapper over the Content Understanding analyzer + analyze operations."""

    def __init__(self, endpoint: str, key: str, api_version: str = "2025-11-01", timeout: float = 60.0, analysis_timeout: float = 900.0):
        if not endpoint or not key:
            raise ContentUnderstandingError("Content Understanding endpoint and key are required.")
        self._base = endpoint.rstrip("/")
        self._api_version = api_version
        self._analysis_timeout = analysis_timeout
        self._client = httpx.AsyncClient(
            timeout=httpx.Timeout(timeout, read=timeout, write=timeout * 5),
            headers={"Ocp-Apim-Subscription-Key": key},
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ContentUnderstandingClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    # -- analyzer lifecycle -------------------------------------------------

    async def get_analyzer(self, analyzer_id: str) -> dict[str, Any] | None:
        url = f"{self._base}/contentunderstanding/analyzers/{analyzer_id}"
        response = await self._client.get(url, params={"api-version": self._api_version})
        if response.status_code == 404:
            return None
        self._raise_for_status(response)
        return response.json()

    async def create_analyzer(self, analyzer_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        url = f"{self._base}/contentunderstanding/analyzers/{analyzer_id}"
        response = await self._client.put(
            url,
            params={"api-version": self._api_version, "allowReplace": "true"},
            json=definition,
        )
        self._raise_for_status(response)
        operation_location = response.headers.get("Operation-Location")
        if operation_location:
            result = await self._poll(
                operation_location, key="status", terminal=TERMINAL_STATES | {"ready", "failed"}
            )
            if str(result.get("status", "")).lower() not in {"succeeded", "ready"}:
                error = result.get("error") or {}
                raise ContentUnderstandingError(
                    _error_message(error, f"Analyzer creation ended with status {result.get('status')}."),
                    payload=result,
                )
        return await self.get_analyzer(analyzer_id) or response.json()

    async def ensure_analyzer(self, analyzer_id: str, definition: dict[str, Any]) -> dict[str, Any]:
        existing = await self.get_analyzer(analyzer_id)
        if existing and existing.get("status") == "ready":
            return existing
        return await self.create_analyzer(analyzer_id, definition)

    async def delete_analyzer(self, analyzer_id: str) -> None:
        url = f"{self._base}/contentunderstanding/analyzers/{analyzer_id}"
        response = await self._client.delete(url, params={"api-version": self._api_version})
        if response.status_code not in (200, 204, 404):
            self._raise_for_status(response)

    # -- analysis -----------------------------------------------------------

    async def analyze_url(self, analyzer_id: str, url: str) -> dict[str, Any]:
        endpoint = f"{self._base}/contentunderstanding/analyzers/{analyzer_id}:analyze"
        response = await self._client.post(
            endpoint,
            params={"api-version": self._api_version},
            json={"inputs": [{"url": url}]},
        )
        return await self._await_analysis(response)

    async def analyze_bytes(self, analyzer_id: str, data: bytes, content_type: str) -> dict[str, Any]:
        endpoint = f"{self._base}/contentunderstanding/analyzers/{analyzer_id}:analyzeBinary"
        response = await self._client.post(
            endpoint,
            params={"api-version": self._api_version},
            content=data,
            headers={"Content-Type": content_type or "application/octet-stream"},
        )
        return await self._await_analysis(response)

    async def _await_analysis(self, response: httpx.Response) -> dict[str, Any]:
        self._raise_for_status(response)
        operation_location = response.headers.get("Operation-Location")
        if not operation_location:
            raise ContentUnderstandingError("Analyze response did not include an Operation-Location header.")
        result = await self._poll(operation_location, key="status", terminal=TERMINAL_STATES, max_wait=self._analysis_timeout)
        if result.get("status") != "Succeeded":
            error = result.get("error") or {}
            raise ContentUnderstandingError(
                _error_message(error, f"Analysis ended with status {result.get('status')}."),
                payload=result,
            )
        return result

    # -- helpers ------------------------------------------------------------

    async def _poll(
        self,
        operation_location: str,
        *,
        key: str,
        terminal: set[str],
        interval: float = 2.0,
        max_wait: float = 900.0,
    ) -> dict[str, Any]:
        waited = 0.0
        while True:
            response = await self._client.get(operation_location)
            self._raise_for_status(response)
            payload = response.json()
            status = str(payload.get(key, "")).strip()
            if status in terminal or status.lower() in {t.lower() for t in terminal}:
                return payload
            if waited >= max_wait:
                raise ContentUnderstandingError(
                    f"Timed out after {max_wait:.0f}s waiting for operation to finish (last status: {status}).",
                    payload=payload,
                )
            await asyncio.sleep(interval)
            waited += interval

    @staticmethod
    def _raise_for_status(response: httpx.Response) -> None:
        if response.is_success:
            return
        try:
            payload = response.json()
            message = _error_message(payload.get("error"), response.text) if isinstance(payload, dict) else response.text
        except Exception:  # noqa: BLE001 - non-JSON error bodies
            payload = response.text
            message = response.text
        raise ContentUnderstandingError(
            f"Content Understanding request failed ({response.status_code}): {message}",
            status_code=response.status_code,
            payload=payload,
        )
