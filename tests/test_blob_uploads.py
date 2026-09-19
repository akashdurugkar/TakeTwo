import time
import unittest
import asyncio
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.blob_uploads import BlobUploads, UploadError
from app.config import Settings
from app.jobs import JobStore
from app.models import VideoSummary
from app.uploads_api import create_router


class UploadSessionTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, blob_account_name="teststorageaccount")
        self.uploads = BlobUploads()

    def test_one_gigabyte_is_reserved_without_reading_file_bytes(self):
        session = self.uploads.reserve("video.mp4", 1024 ** 3, "video/mp4", self.settings)
        self.assertEqual(session.size, 1024 ** 3)
        self.assertEqual(session.blob_name, f"staging/{session.id}/video")
        self.assertEqual(self.uploads.get(session.id), session)

    def test_client_filename_cannot_control_blob_path(self):
        session = self.uploads.reserve("../../other-data.mp4", 1, "video/mp4", self.settings)
        self.assertNotIn("other-data", session.blob_name)
        self.assertNotIn("..", session.blob_name)
        with self.assertRaises(UploadError):
            self.uploads.get("../../other-data.mp4")

    def test_limits_disabled_expired_and_canceled(self):
        for size in (0, -1, 4_000_000_001):
            with self.assertRaises(UploadError):
                self.uploads.reserve("video.mp4", size, "video/mp4", self.settings)
        with self.assertRaises(UploadError):
            self.uploads.reserve("video.mp4", 1, "video/mp4", Settings(_env_file=None))
        session = self.uploads.reserve("video.mp4", 1, "video/mp4", self.settings)
        session.expires = time.time() - 1
        with self.assertRaises(UploadError):
            self.uploads.get(session.id)
        session.expires = time.time() + 10
        session.canceled = True
        with self.assertRaises(UploadError):
            self.uploads.get(session.id)

    def test_pending_sessions_are_bounded(self):
        for _ in range(10):
            self.uploads.reserve("video.mp4", 1, "video/mp4", self.settings)
        with self.assertRaises(UploadError):
            self.uploads.reserve("video.mp4", 1, "video/mp4", self.settings)


class UploadApiTests(unittest.TestCase):
    def setUp(self):
        self.settings = Settings(_env_file=None, blob_account_name="teststorageaccount", cu_endpoint="https://example.invalid", cu_key="test", aoai_endpoint="https://example.invalid", aoai_key="test", aoai_deployment="test")
        self.uploads = BlobUploads()
        self.store = JobStore()
        self.uploads.upload_url = AsyncMock(return_value="https://example.invalid/owned-blob?sig=upload-secret")
        self.uploads.seal = AsyncMock(return_value="https://example.invalid/owned-blob?sig=read-secret")
        self.uploads.cleanup = AsyncMock()

        async def keepalive(*_args):
            await asyncio.Future()

        async def runner(job_id, **kwargs):
            self.store.update(job_id, status="succeeded", video_summary=VideoSummary(summary="Saved extraction"))

        self.uploads.keepalive = keepalive
        self.runner = AsyncMock(side_effect=runner)
        self.app = FastAPI()
        self.app.include_router(create_router(lambda: self.store, lambda: self.settings, lambda: self.uploads, self.runner, {"video/mp4"}))
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def start(self):
        response = self.client.post("/api/uploads", json={"filename": "large.mp4", "size": 1024 ** 3, "content_type": "video/mp4"})
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.headers["cache-control"], "no-store")
        return response.json()["upload_id"]

    def test_one_gb_upload_finalizes_once_and_never_stores_sas_in_job(self):
        upload_id = self.start()
        response = self.client.post(f"/api/uploads/{upload_id}/complete", json={"goal": "Review"})
        self.assertEqual(response.status_code, 202)
        job = self.store.get(response.json()["job_id"])
        self.assertEqual(job.filename, "large.mp4")
        self.assertEqual(job.blob_upload_id, upload_id)
        self.assertFalse(job.blob_upload_active)
        self.assertNotIn("secret", job.model_dump_json())
        self.assertEqual(job.source_url, "")
        self.assertIsNone(self.runner.call_args.kwargs["data"])
        self.assertIn("read-secret", self.runner.call_args.kwargs["source_url"])
        self.uploads.cleanup.assert_awaited_once()
        again = self.client.post(f"/api/uploads/{upload_id}/complete", json={"goal": "Review"})
        self.assertEqual(again.json(), response.json())
        self.runner.assert_awaited_once()
        self.assertEqual(self.client.post(f"/api/uploads/{upload_id}/complete", json={"goal": "Different"}).status_code, 409)
        self.assertEqual(self.client.delete(f"/api/uploads/{upload_id}").status_code, 409)

    def test_validation_and_cancellation_prevent_analysis(self):
        self.assertEqual(self.client.post("/api/uploads", json={"filename": "x.mp4", "size": 4_000_000_001, "content_type": "video/mp4"}).status_code, 400)
        self.assertEqual(self.client.post("/api/uploads", json={"filename": "x.exe", "size": 1, "content_type": "application/octet-stream"}).status_code, 415)
        upload_id = self.start()
        self.assertEqual(self.client.delete(f"/api/uploads/{upload_id}").status_code, 204)
        self.assertEqual(self.client.post(f"/api/uploads/{upload_id}/complete", json={}).status_code, 404)
        self.assertEqual(self.client.delete("/api/uploads/not-owned").status_code, 204)
        self.runner.assert_not_awaited()

    def test_staged_upload_preserves_purpose_and_retry_options(self):
        upload_id = self.start()
        body = {"target_platform": "general", "video_purpose": "Product demo", "goal": "Explain the workflow"}
        response = self.client.post(f"/api/uploads/{upload_id}/complete", json=body)
        self.assertEqual(response.status_code, 202)
        job = self.store.get(response.json()["job_id"])
        self.assertEqual(job.target_platform, "general")
        self.assertEqual(job.video_purpose, "Product demo")
        self.assertEqual(self.client.post(f"/api/uploads/{upload_id}/complete", json=body).json(), response.json())
        changed = self.client.post(f"/api/uploads/{upload_id}/complete", json={**body, "video_purpose": "Presentation"})
        self.assertEqual(changed.status_code, 409)
        self.runner.assert_awaited_once()

    def test_staged_purpose_validation_happens_before_sealing(self):
        upload_id = self.start()
        for purpose in ("", "   ", "x" * 201):
            response = self.client.post(f"/api/uploads/{upload_id}/complete", json={"video_purpose": purpose})
            self.assertEqual(response.status_code, 422)
        self.uploads.seal.assert_not_awaited()
        self.runner.assert_not_awaited()

    def test_mismatch_failure_does_not_start_analysis(self):
        upload_id = self.start()
        self.uploads.seal.side_effect = UploadError("Uploaded size does not match")
        self.assertEqual(self.client.post(f"/api/uploads/{upload_id}/complete", json={}).status_code, 400)
        self.runner.assert_not_awaited()

    def test_cleanup_failure_is_retained_without_exposing_credentials(self):
        upload_id = self.start()
        self.uploads.cleanup.side_effect = RuntimeError("sig=private-secret")
        response = self.client.post(f"/api/uploads/{upload_id}/complete", json={})
        job = self.store.get(response.json()["job_id"])
        self.assertTrue(job.blob_cleanup_pending)
        self.assertNotIn("private-secret", job.model_dump_json())

    def test_authorization_errors_are_sanitized(self):
        self.uploads.upload_url.side_effect = RuntimeError("sig=private-secret")
        response = self.client.post("/api/uploads", json={"filename": "x.mp4", "size": 1, "content_type": "video/mp4"})
        self.assertEqual(response.status_code, 503)
        self.assertNotIn("private-secret", response.text)
        self.assertEqual(self.uploads.sessions, {})

    def test_delete_retries_failed_blob_cleanup_and_never_deletes_external_url(self):
        from app.main import app

        session = self.uploads.reserve("x.mp4", 1, "video/mp4", self.settings)
        job = self.store.create(filename="x.mp4", goal="Review", target_platform="linkedin", blob_upload_id=session.id)
        self.store.update(job.id, status="succeeded", blob_upload_active=False, blob_cleanup_pending=True)
        with patch("app.main.store", self.store), patch("app.main.uploads", self.uploads), patch("app.main.get_settings", return_value=self.settings), TestClient(app) as client:
            self.uploads.cleanup.side_effect = RuntimeError("Storage unavailable")
            self.assertEqual(client.delete(f"/api/jobs/{job.id}").status_code, 503)
            self.assertIsNotNone(self.store.get(job.id))
            self.uploads.cleanup.side_effect = None
            self.assertEqual(client.delete(f"/api/jobs/{job.id}").status_code, 204)
            external = self.store.create(source_url="https://someone-else.blob.core.windows.net/video.mp4", target_platform="linkedin", goal="Review")
            self.store.update(external.id, status="succeeded")
            self.uploads.cleanup.reset_mock()
            self.assertEqual(client.delete(f"/api/jobs/{external.id}").status_code, 204)
            self.uploads.cleanup.assert_not_awaited()

    def test_processing_upload_cannot_be_deleted_or_evicted_during_cleanup(self):
        store = JobStore(max_jobs=1)
        job = store.create(filename="large.mp4", goal="Review", target_platform="linkedin", blob_upload_id="owned-upload")
        store.update(job.id, status="succeeded")
        with self.assertRaises(ValueError):
            store.delete(job.id)
        store.create(goal="Another job", target_platform="linkedin")
        self.assertIsNotNone(store.get(job.id))