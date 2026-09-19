import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.agent.web_search import WebResearch, parse_research, responses_base_url
from app.config import Settings


def search_payload():
    return {
        "status": "completed",
        "output": [
            {"type": "reasoning"},
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "Check the official chart.", "annotations": [
                {"type": "url_citation", "title": "Official chart", "url": "https://example.com/chart", "start_index": 10, "end_index": 24}
            ]}]},
        ],
    }


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def settings(self, **overrides):
        return Settings(_env_file=None, aoai_endpoint="https://example.invalid", aoai_key="fake-test-secret", **overrides)

    def test_endpoint_formats(self):
        for suffix in ("", "/", "/openai/v1", "/openai/v1/", "/openai/v1/responses"):
            self.assertEqual(responses_base_url("https://example.invalid" + suffix), "https://example.invalid/openai/v1/")
        for endpoint in ("http://example.invalid", "https://user:password@example.invalid", "https://example.invalid/api/projects/test"):
            with self.assertRaises(ValueError):
                responses_base_url(endpoint)

    def test_requires_completed_search_and_citations(self):
        payload = search_payload()
        result = parse_research(payload, agent="Music Curator", query="music charts")
        self.assertEqual(result.status, "succeeded")
        self.assertEqual(result.citations[0].url, "https://example.com/chart")
        payload["output"] = payload["output"][2:]
        self.assertEqual(parse_research(payload, agent="Music", query="charts").status, "unavailable")
        payload = search_payload()
        payload["output"][2]["content"][0]["annotations"][0]["url"] = "javascript:alert(1)"
        result = parse_research(payload, agent="Music", query="charts")
        self.assertEqual(result.status, "unavailable")
        self.assertFalse(result.answer)

    def test_incomplete_response_is_not_used_even_with_citations(self):
        payload = search_payload()
        payload["status"] = "incomplete"
        payload["incomplete_details"] = {"reason": "max_output_tokens"}
        result = parse_research(payload, agent="Director", query="trends")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("incomplete", result.error)
        self.assertFalse(result.answer)

    async def test_search_uses_only_query_and_enforces_job_budget(self):
        response = SimpleNamespace(model_dump=lambda: search_payload())
        client = SimpleNamespace(responses=SimpleNamespace(create=AsyncMock(return_value=response)))
        manager = AsyncMock()
        manager.__aenter__.return_value = client
        research = WebResearch(self.settings(web_search_max_calls=1))
        with patch("app.agent.web_search.AsyncOpenAI", return_value=manager):
            first, second = await asyncio.gather(research.search("music charts", agent="Director"), research.search("other trends", agent="Music"))
        self.assertEqual(first.status, "succeeded")
        self.assertEqual(second.status, "unavailable")
        client.responses.create.assert_awaited_once()
        kwargs = client.responses.create.call_args.kwargs
        self.assertEqual(kwargs["input"], "music charts")
        self.assertEqual(kwargs["tools"][0]["type"], "web_search")
        self.assertFalse(kwargs["store"])
        self.assertEqual(kwargs["max_tool_calls"], 1)

    async def test_disabled_and_sensitive_queries_never_call_provider(self):
        with patch("app.agent.web_search.AsyncOpenAI") as client:
            research = WebResearch(self.settings(web_search_enabled=False))
            self.assertEqual((await research.search("trends", agent="Director")).status, "unavailable")
            research = WebResearch(self.settings())
            for query in ("fake-test-secret", "https://private.invalid/file?sig=secret", "user@example.com", "", "x" * 301):
                self.assertEqual((await research.search(query, agent="Director")).status, "unavailable")
            client.assert_not_called()
            self.assertNotIn("fake-test-secret", str(research.results))

    async def test_provider_errors_are_not_exposed(self):
        manager = AsyncMock()
        manager.__aenter__.side_effect = RuntimeError("fake-test-secret")
        with patch("app.agent.web_search.AsyncOpenAI", return_value=manager):
            result = await WebResearch(self.settings()).search("music charts", agent="Director")
        self.assertEqual(result.status, "unavailable")
        self.assertNotIn("fake-test-secret", result.model_dump_json())

    async def test_timeout_reports_unavailable(self):
        manager = AsyncMock()
        manager.__aenter__.side_effect = TimeoutError()
        with patch("app.agent.web_search.AsyncOpenAI", return_value=manager):
            research = WebResearch(self.settings())
            result = await research.search("music charts", agent="Music")
        self.assertEqual(result.status, "unavailable")
        self.assertIn("timed out", result.error)
        self.assertEqual(research.results, [result])

    async def test_cancellation_is_recorded_and_propagated(self):
        manager = AsyncMock()
        manager.__aenter__.side_effect = asyncio.CancelledError()
        research = WebResearch(self.settings())
        with patch("app.agent.web_search.AsyncOpenAI", return_value=manager), self.assertRaises(asyncio.CancelledError):
            await research.search("music charts", agent="Music")
        self.assertEqual(len(research.results), 1)
        self.assertIn("cancelled", research.results[0].error)

    def test_citation_offsets_are_preserved_across_message_blocks(self):
        payload = search_payload()
        payload["output"].insert(2, {"type": "message", "content": [{"type": "output_text", "text": "First result.", "annotations": []}]})
        result = parse_research(payload, agent="Music", query="trends")
        self.assertEqual(result.answer, "First result.\nCheck the official chart.\n")
        citation = result.citations[0]
        self.assertEqual(result.answer[citation.start_index:citation.end_index], "official chart")