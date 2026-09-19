"""Large local videos are uploaded directly to private, temporary Blob storage."""

import asyncio
import logging
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field

from app.blob_uploads import UploadError
from app.models import Platform, VideoPurpose

logger = logging.getLogger(__name__)


class UploadRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    filename: str = Field(min_length=1, max_length=255)
    size: int = Field(gt=0, strict=True)
    content_type: str = Field(max_length=100)


class CompleteRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_platform: Platform = "general"
    video_purpose: VideoPurpose = "General review"
    goal: str = Field("", max_length=500)
    use_web_search: bool = True


def create_router(get_store, get_settings, get_uploads, runner, allowed_types):
    router = APIRouter(prefix="/api/uploads")

    async def process(job_id, session, settings, source_url):
        uploads = get_uploads()
        running = asyncio.create_task(runner(job_id, data=None, content_type=session.content_type, source_url=source_url))
        renewing = asyncio.create_task(uploads.keepalive(session, settings))
        try:
            done, _ = await asyncio.wait([running, renewing], return_when=asyncio.FIRST_COMPLETED)
            if renewing in done:
                await renewing
                raise RuntimeError("Upload lease ended unexpectedly")
            await running
        except asyncio.CancelledError:
            get_store().update(job_id, status="failed", error="Analysis interrupted. Please upload the file again.")
            raise
        except Exception as error:
            logger.warning("Staged analysis failed (%s)", type(error).__name__)
            get_store().update(job_id, status="failed", error="Staged video processing failed. Please upload the file again.")
        finally:
            running.cancel()
            renewing.cancel()
            await asyncio.gather(running, renewing, return_exceptions=True)
            try:
                await uploads.cleanup(session, settings)
                get_store().update(job_id, blob_upload_active=False, blob_cleanup_pending=False)
            except Exception as error:
                logger.warning("Staged upload cleanup failed (%s)", type(error).__name__)
                get_store().update(job_id, blob_upload_active=False, blob_cleanup_pending=True)

    @router.post("", status_code=201)
    async def start(request: UploadRequest, response: Response):
        settings = get_settings()
        if not settings.blob_configured:
            raise HTTPException(503, "Large-file uploads are not configured.")
        if not settings.cu_configured or not settings.aoai_configured:
            raise HTTPException(503, "Configure Content Understanding and Azure OpenAI before uploading.")
        if request.content_type not in allowed_types:
            raise HTTPException(415, "Unsupported video type. Choose an mp4, mov, webm or mkv file.")
        uploads = get_uploads()
        try:
            session = uploads.reserve(Path(request.filename).name, request.size, request.content_type, settings)
        except UploadError as error:
            raise HTTPException(400, str(error)) from None
        try:
            url = await uploads.upload_url(session, settings)
        except Exception as error:
            uploads.sessions.pop(session.id, None)
            logger.warning("Upload authorization failed (%s)", type(error).__name__)
            raise HTTPException(503, "Could not authorize Blob upload. Check the app's Azure login and storage permissions.") from None
        response.headers["Cache-Control"] = "no-store"
        return {"upload_id": session.id, "upload_url": url, "block_size": 8 * 1024 * 1024, "expires_at": session.expires}

    @router.post("/{upload_id}/complete", status_code=202)
    async def complete(upload_id: str, request: CompleteRequest, tasks: BackgroundTasks, response: Response):
        uploads = get_uploads()
        try:
            session = uploads.get(upload_id)
        except UploadError as error:
            raise HTTPException(404, str(error)) from None
        async with session.lock:
            if session.canceled:
                raise HTTPException(409, "Upload was canceled.")
            signature = request.model_dump_json()
            if session.job_id:
                if session.request_signature != signature:
                    raise HTTPException(409, "This upload already started analysis with different options.")
                if get_store().get(session.job_id) is None:
                    raise HTTPException(410, "The report for this upload is no longer available.")
            else:
                settings = get_settings()
                try:
                    url = await uploads.seal(session, settings)
                except UploadError as error:
                    raise HTTPException(400, str(error)) from None
                except Exception as error:
                    logger.warning("Upload finalization failed (%s)", type(error).__name__)
                    raise HTTPException(503, "Could not finalize the upload. Retry or cancel it.") from None
                job = get_store().create(
                    filename=session.filename, target_platform=request.target_platform,
                    video_purpose=request.video_purpose,
                    goal=request.goal.strip() or "General video review",
                    use_web_search=request.use_web_search and settings.web_search_enabled,
                    blob_upload_id=session.id,
                )
                session.job_id = job.id
                session.request_signature = signature
                tasks.add_task(process, job.id, session, settings, url)
        response.headers["Cache-Control"] = "no-store"
        return {"job_id": session.job_id}

    @router.delete("/{upload_id}", status_code=204)
    async def cancel(upload_id: str):
        uploads = get_uploads()
        session = uploads.sessions.get(upload_id)
        if session is None:
            return Response(status_code=204, headers={"Cache-Control": "no-store"})
        async with session.lock:
            if session.job_id:
                raise HTTPException(409, "Analysis already started. Use the report's delete action after it finishes.")
            session.canceled = True
        try:
            await uploads.cleanup(session, get_settings())
        except Exception as error:
            logger.warning("Canceled upload cleanup failed (%s)", type(error).__name__)
            raise HTTPException(503, "Upload canceled, but Blob cleanup failed. Retry cancellation.") from None
        return Response(status_code=204, headers={"Cache-Control": "no-store"})

    return router