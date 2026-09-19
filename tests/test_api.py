import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobStore
from app.main import app
from app.models import DirectorPlan, DirectorReview, VideoSummary


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(
            _env_file=None,
            cu_endpoint="https://example.invalid",
            cu_key="test-only",
            aoai_endpoint="https://example.invalid",
            aoai_key="test-only",
            aoai_deployment="test-model",
        )
        self.summary = VideoSummary(summary="A short demonstration", duration_ms=10000)
        self.analysis = AsyncMock(return_value=(self.summary, "test-analyzer"))
        self.agent = SimpleNamespace(run=AsyncMock(return_value=SimpleNamespace(
            value=DirectorPlan(headline="A short demonstration", review=DirectorReview(
                grounding_checked=True, request_checked=True, reviewed_specialists=[],
            ))
        )))
        self.store = JobStore()
        for patcher in (
            patch("app.main.get_settings", return_value=self.settings),
            patch("app.main.store", new=self.store),
            patch("app.pipeline.analyze_video", new=self.analysis),
            patch("app.agent.orchestrator.build_client", return_value=object()),
            patch("app.agent.orchestrator.build_agent", return_value=self.agent),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_upload_flows_through_analysis_director_and_serialization(self):
        response = self.client.post("/api/analyze", data={"goal": "Summarize this video"}, files={
            "file": ("reel.mp4", b"test-video", "video/mp4")
        })
        self.assertEqual(response.status_code, 202)
        job = self.client.get(f"/api/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["analyzer_id"], "test-analyzer")
        self.assertEqual(job["report"]["headline"], "A short demonstration")
        self.assertIsNone(job["report"]["overall_score"])
        self.assertEqual([run["status"] for run in job["agent_runs"]], ["succeeded", "skipped", "skipped", "skipped"])
        self.analysis.assert_awaited_once()
        self.assertEqual(self.analysis.call_args.kwargs["data"], b"test-video")
        self.agent.run.assert_awaited_once()

    def test_url_flows_to_analysis(self):
        response = self.client.post("/api/analyze", data={"video_url": "https://example.invalid/reel.mp4"})
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.analysis.call_args.kwargs["source_url"], "https://example.invalid/reel.mp4")
        self.assertIsNone(self.analysis.call_args.kwargs["data"])
        job = self.client.get(f"/api/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["goal"], "General video review")
        self.assertEqual(job["target_platform"], "general")
        self.assertEqual(job["video_purpose"], "General review")

    def test_video_purpose_is_separate_from_platform_and_reaches_director(self):
        response = self.client.post("/api/analyze", data={
            "video_url": "https://example.invalid/demo.mp4", "target_platform": "website",
            "video_purpose": " Product demo ", "goal": "Explain the workflow",
        })
        self.assertEqual(response.status_code, 202)
        job = self.client.get(f"/api/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["video_purpose"], "Product demo")
        self.assertEqual(job["target_platform"], "website")
        prompt = self.agent.run.call_args.args[0]
        self.assertIn('Video purpose (creator-provided context, not evidence): "Product demo"', prompt)
        self.assertIn("Target platform: website", prompt)

    def test_custom_purpose_and_platform_neutral_review(self):
        response = self.client.post("/api/analyze", data={
            "video_purpose": "Internal stakeholder update", "target_platform": "general",
        }, files={"file": ("update.mp4", b"sample", "video/mp4")})
        self.assertEqual(response.status_code, 202)
        prompt = self.agent.run.call_args.args[0]
        self.assertIn("Internal stakeholder update", prompt)
        self.assertIn("Do not assume a social feed", prompt)
        platforms = self.client.get("/api/platforms").json()
        self.assertEqual(platforms["general"], "No specific platform / Other")
        self.assertEqual(platforms["youtube"], "YouTube")

    def test_oversized_purpose_is_rejected_before_analysis(self):
        response = self.client.post("/api/analyze", data={
            "video_url": "https://example.invalid/demo.mp4", "video_purpose": "x" * 201,
        })
        self.assertEqual(response.status_code, 422)
        self.analysis.assert_not_awaited()

    def test_missing_configuration_is_blocked_before_analysis(self):
        with patch("app.main.get_settings", return_value=self.settings.model_copy(update={"cu_key": ""})):
            response = self.client.post("/api/analyze", data={"video_url": "https://example.invalid/reel.mp4"})
        self.assertEqual(response.status_code, 503)
        self.analysis.assert_not_awaited()

    def test_youtube_page_links_are_rejected_before_analysis(self):
        for url in (
            "https://www.youtube.com/watch?v=example",
            "https://youtu.be/example",
            "https://m.youtube.com/shorts/example",
            "https://www.youtube-nocookie.com/embed/example",
            "https://WWW.YOUTUBE.COM./watch?v=example",
        ):
            with self.subTest(url=url):
                response = self.client.post("/api/analyze", data={"video_url": url})
                self.assertEqual(response.status_code, 400)
                self.assertIn("not direct video files", response.json()["detail"])
        self.analysis.assert_not_awaited()

    def test_invalid_url_is_rejected_before_analysis(self):
        for url in ("https://", "https://[broken", "file:///reel.mp4"):
            with self.subTest(url=url):
                response = self.client.post("/api/analyze", data={"video_url": url})
                self.assertEqual(response.status_code, 400)
        self.analysis.assert_not_awaited()

    def test_direct_url_does_not_require_file_extension(self):
        response = self.client.post("/api/analyze", data={"video_url": "https://example.invalid/download?id=video"})
        self.assertEqual(response.status_code, 202)
        self.analysis.assert_awaited_once()

    def test_director_failure_marks_job_failed(self):
        self.agent.run.side_effect = RuntimeError("Simulated model failure")
        with self.assertLogs(level="ERROR"):
            response = self.client.post("/api/analyze", data={"video_url": "https://example.invalid/reel.mp4"})
        job = self.client.get(f"/api/jobs/{response.json()['job_id']}").json()
        self.assertEqual(job["status"], "failed")
        self.assertIsNone(job["report"])
        self.assertEqual(job["agent_runs"][0]["status"], "failed")

    def test_api_and_static_routes(self):
        for path in ("/", "/api/health", "/api/platforms", "/static/app.js", "/static/styles.css", "/static/director-chat.js", "/static/vendor/marked.umd.js", "/static/vendor/purify.min.js"):
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)
        self.assertEqual(self.client.get("/api/jobs/missing").status_code, 404)
        self.assertIn('value="General video review"', self.client.get("/").text)

    def test_chat_workspace_has_tabs_compact_composer_and_optional_controls(self):
        page = self.client.get("/").text
        for control in ("report-tab", "chat-tab", "chat-scroll", "chat-empty", "chat-edit-goal", "chat-goal-editor", "chat-options", "chat-message", "chat-send", "chat-latest", "chat-web-search", "chat-reextract"):
            self.assertIn(f'id="{control}"', page)
        self.assertIn('role="tablist"', page)
        self.assertIn('aria-controls="director-chat"', page)
        self.assertIn('id="chat-goal-editor" class="chat-goal-editor hidden"', page)
        self.assertIn('class="chat-composer"', page)
        self.assertLess(page.index('/static/vendor/purify.min.js'), page.index('/static/director-chat.js'))

    def test_taketwo_branding_preserves_platform_and_analyzer_contracts(self):
        page = self.client.get("/").text
        self.assertIn("<h1>TakeTwo</h1>", page)
        self.assertIn("<title>TakeTwo - Video analysis", page)
        self.assertIn("Analyze video", page)
        self.assertIn("Drop a video here", page)
        self.assertIn("Video improvement plan", page)
        for old_label in ("MakeItReel", "ReelPilot", "Analyze reel", "vertical video"):
            self.assertNotIn(old_label, page)
        self.assertEqual(self.client.get("/openapi.json").json()["info"]["title"], "TakeTwo")
        self.assertEqual(self.client.get("/api/platforms").json()["instagram_reels"], "Instagram Reels")
        self.assertEqual(Settings.model_fields["cu_analyzer_id"].default, "MakeItReelAnalyzer")
        self.assertIn("Drop a video here", self.client.get("/static/app.js").text)

    def test_delete_completed_job_removes_report_and_is_idempotent(self):
        response = self.client.post("/api/analyze", data={"goal": "Summarize this video"}, files={
            "file": ("reel.mp4", b"test-video", "video/mp4")
        })
        job_id = response.json()["job_id"]
        self.assertIsNotNone(self.store.get(job_id).report)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").headers["cache-control"], "no-store")
        other_job = self.store.create(target_platform="instagram_reels", goal="Other request")

        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/jobs/{job_id}").status_code, 404)
        self.assertIsNone(self.store.get(job_id))
        self.assertEqual([job.id for job in self.store.list()], [other_job.id])
        self.assertIsNone(self.store.update(job_id, stage_message="Late update"))
        self.assertEqual(self.client.delete(f"/api/jobs/{job_id}").status_code, 204)
        self.analysis.assert_awaited_once()
        self.agent.run.assert_awaited_once()

    def test_delete_failed_job(self):
        job = self.store.create(target_platform="instagram_reels", goal="A failed request")
        self.store.update(job.id, status="failed", error="Failed", video_summary=self.summary)
        self.assertEqual(self.client.delete(f"/api/jobs/{job.id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/jobs/{job.id}").status_code, 404)
        self.analysis.assert_not_awaited()

    def test_delete_active_job_is_rejected(self):
        for status in ("queued", "analyzing", "briefing", "advising", "synthesizing"):
            with self.subTest(status=status):
                job = self.store.create(target_platform="instagram_reels", goal="Active request")
                self.store.update(job.id, status=status)
                response = self.client.delete(f"/api/jobs/{job.id}")
                self.assertEqual(response.status_code, 409)
                self.assertIn("still running", response.json()["detail"])
                self.assertEqual(self.store.get(job.id).status, status)
        self.analysis.assert_not_awaited()

    def test_delete_missing_job_is_already_complete(self):
        response = self.client.delete("/api/jobs/missing")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(response.content, b"")
        self.assertEqual(response.headers["cache-control"], "no-store")

    def test_web_research_opt_out_reaches_agents(self):
        response = self.client.post("/api/analyze", data={
            "video_url": "https://example.invalid/reel.mp4", "use_web_search": "false",
        })
        job = self.client.get(f"/api/jobs/{response.json()['job_id']}").json()
        self.assertFalse(job["use_web_search"])
        tools = self.agent.run.call_args.kwargs["tools"]
        self.assertNotIn("search_web", [tool.name for tool in tools])

    def test_server_can_disable_web_research(self):
        self.settings.web_search_enabled = False
        response = self.client.post("/api/analyze", data={
            "video_url": "https://example.invalid/reel.mp4", "use_web_search": "true",
        })
        job = self.client.get(f"/api/jobs/{response.json()['job_id']}").json()
        self.assertFalse(job["use_web_search"])
        self.assertFalse(self.client.get("/api/health").json()["web_search_enabled"])