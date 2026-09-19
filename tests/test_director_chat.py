import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

from fastapi.testclient import TestClient

from app.config import Settings
from app.jobs import JobStore
from app.main import app
from app.models import CaptionSuggestion, CopyPack, DirectorChatTurn, ReelPlan, VideoSummary


class DirectorChatTests(unittest.TestCase):
    def setUp(self):
        self.store = JobStore()
        self.settings = Settings(_env_file=None, cu_endpoint="", cu_key="", aoai_endpoint="https://example.invalid", aoai_key="test-only")
        self.summary = VideoSummary(summary="A fabric demonstration", duration_ms=9000)
        self.original = ReelPlan(headline="Improve the opening")
        job = self.store.create(target_platform="youtube_shorts", goal="General video review")
        self.store.update(job.id, status="succeeded", video_summary=self.summary, report=self.original)
        self.job_id = job.id
        self.agent = AsyncMock(return_value=ReelPlan(headline="Explain the opening", response="The opening needs context, not more cuts."))
        self.cu = AsyncMock(side_effect=AssertionError("Chat must never extract the video"))
        for patcher in (
            patch("app.main.store", new=self.store),
            patch("app.main.get_settings", return_value=self.settings),
            patch("app.director_chat.run_creative_team", new=self.agent),
            patch("app.pipeline.analyze_video", new=self.cu),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def send(self, **body):
        return self.client.post(f"/api/jobs/{self.job_id}/chat", json=body)

    def conversation(self):
        response = self.client.get(f"/api/jobs/{self.job_id}/chat")
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()

    def test_chat_and_goal_changes_reuse_extraction_and_preserve_original(self):
        self.assertEqual(self.send(message="Why change the opening?").status_code, 202)
        self.assertEqual(self.send(message="Make it educational", goal="Teach fabric selection").status_code, 202)
        self.assertEqual(self.send(message="Shorten your suggestion").status_code, 202)
        conversation = self.conversation()
        self.assertEqual(conversation["goal"], "Teach fabric selection")
        self.assertEqual(len(conversation["turns"]), 3)
        self.assertTrue(all(turn["status"] == "succeeded" for turn in conversation["turns"]))
        self.assertEqual(self.agent.call_args.args[1], self.summary)
        self.assertEqual(self.agent.call_args.args[3], "Teach fabric selection")
        context = self.agent.call_args.kwargs["conversation_context"]
        self.assertIn("Why change the opening?", context)
        self.assertIn("The opening needs context", context)
        self.assertIn("Improve the opening", context)
        self.assertFalse(self.agent.call_args.kwargs["allow_web_search"])
        job = self.store.get(self.job_id)
        self.assertEqual(job.video_summary, self.summary)
        self.assertEqual(job.report, self.original)
        self.assertEqual(job.goal, "General video review")
        self.cu.assert_not_awaited()

    def test_explicit_reextract_message_does_not_automatically_call_cu(self):
        self.assertEqual(self.send(message="Re-extract the video").status_code, 202)
        self.cu.assert_not_awaited()
        self.agent.assert_awaited_once()

    def test_goal_only_turn_and_idempotent_retry(self):
        request_id = str(uuid4())
        body = {"goal": "Product education", "request_id": request_id}
        self.assertEqual(self.send(**body).status_code, 202)
        self.assertEqual(self.send(**body).status_code, 202)
        self.agent.assert_awaited_once()
        self.assertEqual(len(self.conversation()["turns"]), 1)
        self.assertEqual(self.send(message="Different request", request_id=request_id).status_code, 409)

    def test_active_turn_blocks_another_turn_and_delete(self):
        self.store.begin_chat(self.job_id, DirectorChatTurn(id="active", message="Question", goal="Review", created_at="test"))
        self.assertEqual(self.send(message="Another question").status_code, 409)
        self.assertEqual(self.client.delete(f"/api/jobs/{self.job_id}").status_code, 409)
        self.agent.assert_not_awaited()

    def test_failed_turn_can_be_retried_and_goal_is_not_committed(self):
        self.agent.side_effect = RuntimeError("private-error-detail")
        self.assertEqual(self.send(message="Revise", goal="New goal").status_code, 202)
        conversation = self.conversation()
        self.assertEqual(conversation["turns"][0]["status"], "failed")
        self.assertNotIn("private-error-detail", str(conversation))
        self.assertEqual(conversation["goal"], "General video review")
        self.agent.side_effect = None
        self.assertEqual(self.send(message="Retry", goal="New goal").status_code, 202)
        self.assertEqual(self.conversation()["goal"], "New goal")
        self.cu.assert_not_awaited()

    def test_no_extraction_and_active_analysis_are_rejected(self):
        self.store.update(self.job_id, video_summary=None, status="failed")
        self.assertEqual(self.send(message="Explain").status_code, 409)
        self.store.update(self.job_id, video_summary=self.summary, status="advising")
        self.assertEqual(self.send(message="Explain").status_code, 409)
        self.agent.assert_not_awaited()

    def test_failed_recommendations_with_saved_extraction_can_chat(self):
        self.store.update(self.job_id, status="failed", report=None)
        self.assertEqual(self.send(message="Try recommendations again").status_code, 202)
        self.cu.assert_not_awaited()

    def test_validation_missing_job_and_disabled_search(self):
        for body in ({"message": "   "}, {"message": "x" * 2001}, {"goal": " "}, {"message": "hello", "video_url": "https://example.invalid/video.mp4"}):
            self.assertEqual(self.send(**body).status_code, 422)
        self.settings.web_search_enabled = False
        self.send(message="Research", use_web_search=True)
        self.assertFalse(self.agent.call_args.kwargs["allow_web_search"])
        self.assertEqual(self.client.get("/api/jobs/missing/chat").status_code, 404)
        self.assertEqual(self.client.post("/api/jobs/missing/chat", json={"message": "Hi"}).status_code, 404)

    def test_delete_removes_conversation_and_late_updates_do_not_restore_it(self):
        self.send(message="Explain")
        self.assertEqual(self.client.delete(f"/api/jobs/{self.job_id}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/jobs/{self.job_id}/chat").status_code, 404)
        self.store.update_chat_turn(self.job_id, "missing", status="succeeded")
        self.assertIsNone(self.store.get(self.job_id))

    def test_earlier_revision_remains_in_context_after_explanation(self):
        self.agent.return_value = ReelPlan(headline="Caption revision", response="Here is revised copy", copywriting=CopyPack(
            captions=[CaptionSuggestion(text="Earlier revised caption")],
        ))
        self.send(message="Write a caption")
        self.agent.return_value = ReelPlan(headline="Explanation", response="I used a simple tone.")
        self.send(message="Explain the tone")
        self.send(message="Shorten that earlier caption")
        self.assertIn("Earlier revised caption", self.agent.call_args.kwargs["conversation_context"])
        self.cu.assert_not_awaited()

    def test_conversations_do_not_cross_jobs(self):
        self.send(message="Private first-job question")
        other = self.store.create(target_platform="linkedin", goal="Other goal")
        self.store.update(other.id, status="succeeded", video_summary=VideoSummary(summary="Other video"))
        response = self.client.post(f"/api/jobs/{other.id}/chat", json={"message": "Discuss this video"})
        self.assertEqual(response.status_code, 202)
        self.assertNotIn("Private first-job question", self.agent.call_args.kwargs["conversation_context"])
        self.assertEqual(self.agent.call_args.args[1].summary, "Other video")

    def test_limits_reject_without_model_or_extraction_calls(self):
        with patch("app.director_chat.MAX_CONTEXT_CHARACTERS", 1):
            self.assertEqual(self.send(message="Explain").status_code, 409)
        self.assertEqual(self.conversation()["turns"], [])
        for index in range(20):
            self.store.begin_chat(self.job_id, DirectorChatTurn(id=str(index), message="Hi", goal="Review", created_at="test"))
            self.store.update_chat_turn(self.job_id, str(index), status="succeeded", answer="Done")
        self.assertEqual(self.send(message="Another question").status_code, 409)
        self.agent.assert_not_awaited()
        self.cu.assert_not_awaited()

    def test_active_chat_is_not_evicted_by_another_upload(self):
        self.store._max_jobs = 1
        self.store.begin_chat(self.job_id, DirectorChatTurn(id="active", message="Question", goal="Review", created_at="test"))
        self.store.create(target_platform="youtube_shorts", goal="New job")
        self.assertIsNotNone(self.store.get(self.job_id))

    def test_cu_client_is_never_constructed_for_chat(self):
        with patch("app.cu.client.ContentUnderstandingClient.__init__", side_effect=AssertionError("Unexpected CU access")):
            response = self.send(message="Explain these recommendations", goal="Education")
        self.assertEqual(response.status_code, 202)
        self.assertEqual(self.conversation()["turns"][0]["status"], "succeeded")

    def test_upload_extracts_once_then_multiple_turns_only_call_director(self):
        extraction = AsyncMock(return_value=(self.summary, "test-analyzer"))
        with patch("app.pipeline.analyze_video", new=extraction), patch(
            "app.pipeline.run_creative_team", new=AsyncMock(return_value=self.original)
        ) as initial_director:
            self.settings.cu_endpoint = "https://example.invalid"
            self.settings.cu_key = "test-only"
            response = self.client.post("/api/analyze", files={"file": ("video.mp4", b"test-video", "video/mp4")})
            self.assertEqual(response.status_code, 202)
            self.job_id = response.json()["job_id"]
            self.send(message="Why that recommendation?")
            self.send(goal="Educational content")
            extraction.assert_awaited_once()
            initial_director.assert_awaited_once()
            self.assertEqual(self.agent.await_count, 2)
            self.assertEqual(self.store.get(self.job_id).video_summary, self.summary)