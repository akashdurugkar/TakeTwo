import asyncio
import unittest
from unittest.mock import AsyncMock, patch

import httpx

from app.config import Settings
from app.cu.analyzer_schema import reel_analyzer_definition
from app.cu.client import ContentUnderstandingClient, ContentUnderstandingError, safe_error_detail


class AnalyzerDefinitionTests(unittest.TestCase):
    def test_defaults_use_supported_video_base_and_custom_id(self):
        defaults = Settings.model_fields
        self.assertEqual(defaults["cu_base_analyzer_id"].default, "prebuilt-video")
        self.assertEqual(defaults["cu_fallback_base_analyzer_id"].default, "")
        self.assertEqual(defaults["cu_prebuilt_analyzer_id"].default, "prebuilt-videoSearch")
        self.assertNotIn("-", defaults["cu_analyzer_id"].default)

    def test_video_fields_use_supported_generation_methods(self):
        definition = reel_analyzer_definition("prebuilt-video")
        fields = definition["fieldSchema"]["fields"]
        self.assertEqual(len(fields), 13)
        self.assertTrue(definition["config"]["returnDetails"])
        self.assertEqual(definition["models"], {"completion": "prebuilt-analyzer-completion"})

        def check_field(field):
            self.assertIn(field.get("method", "generate"), {"generate", "classify"})
            for child in field.get("properties", {}).values():
                check_field(child)
            if "items" in field:
                check_field(field["items"])

        for name, field in fields.items():
            with self.subTest(field=name):
                check_field(field)


class AnalyzerCreationTests(unittest.IsolatedAsyncioTestCase):
    async def create_with_status(self, status):
        paths = []

        def respond(request):
            paths.append(request.url.path)
            if request.method == "PUT":
                return httpx.Response(201, json={"status": "creating"}, headers={
                    "Operation-Location": "https://example.invalid/contentunderstanding/analyzers/reel/operations/create"
                })
            if request.url.path.endswith("/operations/create"):
                return httpx.Response(200, json={"status": status})
            return httpx.Response(200, json={"status": "ready", "analyzerId": "reel"})

        transport_client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch("app.cu.client.httpx.AsyncClient", return_value=transport_client):
            async with ContentUnderstandingClient("https://example.invalid", "test-only") as client:
                result = await asyncio.wait_for(client.create_analyzer("reel", {}), timeout=0.2)
        return result, paths

    async def test_succeeded_operation_fetches_ready_analyzer(self):
        result, paths = await self.create_with_status("Succeeded")
        self.assertEqual(result["status"], "ready")
        self.assertEqual(len(paths), 3)

    async def test_ready_status_is_also_supported(self):
        result, _ = await self.create_with_status("ready")
        self.assertEqual(result["status"], "ready")

    async def test_failed_or_canceled_creation_is_an_error(self):
        for status in ("Failed", "Canceled"):
            with self.subTest(status=status), self.assertRaisesRegex(ContentUnderstandingError, status):
                await self.create_with_status(status)

    async def test_existing_analyzer_is_reused(self):
        async with ContentUnderstandingClient("https://example.invalid", "test-only") as client:
            with patch.object(client, "get_analyzer", new=AsyncMock(return_value={"status": "ready"})), patch.object(
                client, "create_analyzer", new=AsyncMock()
            ) as create:
                result = await client.ensure_analyzer("reel", {})
        self.assertEqual(result["status"], "ready")
        create.assert_not_awaited()


class ErrorDetailsTests(unittest.IsolatedAsyncioTestCase):
    def test_safe_diagnostics_keep_cu_reason_without_credentials_or_signed_urls(self):
        error = ContentUnderstandingError(
            "InvalidContent: Unsupported codec H.265. "
            "https://account.blob.core.windows.net/private/video?sig=secret&amp;sp=r "
            "https%3A%2F%2Faccount.blob.core.windows.net%2Fvideo%3Fsig%3Dencoded "
            "sig=bare-secret api-key=key-secret Authorization=Bearer-secret "
            "secret-key-value secret%2Fencoded\nInjected log line"
        )
        detail = safe_error_detail(error, secrets=("secret-key-value", "secret/encoded"))
        self.assertIn("InvalidContent: Unsupported codec H.265", detail)
        for private in ("account.blob", "bare-secret", "key-secret", "Bearer-secret", "secret-key-value", "secret%2Fencoded", "\n"):
            self.assertNotIn(private, detail)
        self.assertLessEqual(len(safe_error_detail(ValueError("x" * 3000))), 2000)

    async def test_staged_failure_preserves_sanitized_cu_details_in_job_and_log(self):
        from app.jobs import JobStore
        from app.pipeline import run_job

        settings = Settings(_env_file=None, cu_key="private-cu-key", aoai_key="private-model-key")
        store = JobStore()
        job = store.create(filename="large.mp4", target_platform="linkedin", goal="Review", blob_upload_id="owned-upload")
        error = ContentUnderstandingError(
            "Content Understanding request failed (400): InvalidRequest: Invalid request.; "
            "InvalidImageDimension: Invalid video dimensions. Expected min 240x240 and max 1920x1920 pixels. Actual: 3840x2160. "
            "https://example.invalid/video?sig=private-signature private-cu-key",
            status_code=400,
        )
        with patch("app.pipeline.analyze_video", new=AsyncMock(side_effect=error)), patch("app.pipeline.run_creative_team", new=AsyncMock()) as director, self.assertLogs("app.pipeline", level="ERROR") as logs:
            await run_job(store, job.id, settings, source_url="https://example.invalid/video?sig=private-signature")
        result = store.get(job.id)
        self.assertEqual(result.status, "failed")
        self.assertIn("(400): InvalidRequest", result.error)
        self.assertIn("InvalidImageDimension", result.error)
        self.assertIn("Actual: 3840x2160", result.error)
        self.assertIn("1920x1080 for landscape", result.error)
        self.assertIn("Reducing file size alone will not fix this", result.error)
        for output in (result.model_dump_json(), " ".join(logs.output)):
            self.assertNotIn("private-signature", output)
            self.assertNotIn("private-cu-key", output)
            self.assertNotIn("example.invalid", output)
        director.assert_not_awaited()

    async def test_staged_director_failure_is_not_reported_as_a_video_format_issue(self):
        from app.jobs import JobStore
        from app.models import VideoSummary
        from app.pipeline import run_job

        store = JobStore()
        job = store.create(target_platform="linkedin", goal="Review", blob_upload_id="owned-upload")
        with patch("app.pipeline.analyze_video", new=AsyncMock(return_value=(VideoSummary(summary="Saved"), "test"))), patch("app.pipeline.run_creative_team", new=AsyncMock(side_effect=RuntimeError("private details"))), self.assertLogs("app.pipeline", level="ERROR"):
            await run_job(store, job.id, Settings(_env_file=None), source_url="https://example.invalid/video")
        self.assertEqual(store.get(job.id).error, "Creative recommendations failed (RuntimeError).")
        self.assertIsNotNone(store.get(job.id).video_summary)

    def error_payload(self):
        return {"error": {
            "code": "InvalidRequest",
            "message": "Invalid request.",
            "innererror": {
                "code": "InvalidFieldSchema",
                "message": "Schema validation failed.",
                "details": [{
                    "code": "UnsupportedGenerationMethod",
                    "message": "Extract is not supported for video.",
                    "target": "/fieldSchema/fields/OnScreenText",
                }],
            },
        }}

    def test_http_error_includes_nested_reason_and_target(self):
        payload = self.error_payload()
        response = httpx.Response(400, json=payload)
        with self.assertRaises(ContentUnderstandingError) as caught:
            ContentUnderstandingClient._raise_for_status(response)
        self.assertIn("UnsupportedGenerationMethod", str(caught.exception))
        self.assertIn("Extract is not supported for video", str(caught.exception))
        self.assertIn("/fieldSchema/fields/OnScreenText", str(caught.exception))
        self.assertEqual(caught.exception.status_code, 400)
        self.assertEqual(caught.exception.payload, payload)

    def test_non_json_error_preserves_status_and_text(self):
        with self.assertRaisesRegex(ContentUnderstandingError, "503.*Service unavailable"):
            ContentUnderstandingClient._raise_for_status(httpx.Response(503, text="Service unavailable"))

    async def test_failed_analysis_includes_nested_error(self):
        async with ContentUnderstandingClient("https://example.invalid", "test-only") as client:
            result = {"status": "Failed", **self.error_payload()}
            with patch.object(client, "_poll", new=AsyncMock(return_value=result)):
                with self.assertRaisesRegex(ContentUnderstandingError, "UnsupportedGenerationMethod"):
                    await client._await_analysis(httpx.Response(202, headers={
                        "Operation-Location": "https://example.invalid/result"
                    }))