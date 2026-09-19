"""TakeTwo API: upload a video, get content analysis and a creative improvement plan."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Annotated, get_args
from urllib.parse import urlsplit

from fastapi import BackgroundTasks, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

from app.config import get_settings
from app.blob_uploads import uploads
from app.uploads_api import create_router as create_upload_router
from app.director_chat import create_router as create_chat_router
from app.jobs import JobStore
from app.models import Job, Platform, VideoPurpose
from app.pipeline import run_job

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

ROOT_DIR = Path(__file__).resolve().parent.parent
WEB_DIR = ROOT_DIR / "web"

ALLOWED_VIDEO_TYPES = {
    "video/mp4",
    "video/quicktime",
    "video/x-m4v",
    "video/webm",
    "video/x-matroska",
    "video/mpeg",
    "video/3gpp",
}
ALLOWED_PLATFORMS = set(get_args(Platform))

PLATFORM_NAMES = {
    "general": "No specific platform / Other",
    "instagram_reels": "Instagram Reels",
    "tiktok": "TikTok",
    "youtube_shorts": "YouTube Shorts",
    "linkedin": "LinkedIn Video",
    "youtube": "YouTube",
    "website": "Website / landing page",
}

app = FastAPI(title="TakeTwo", version="1.0.0")
store = JobStore()
app.include_router(create_chat_router(lambda: store, lambda: get_settings()))


@app.get("/api/health")
async def health() -> dict[str, object]:
    settings = get_settings()
    return {
        "status": "ok",
        "content_understanding_configured": settings.cu_configured,
        "agent_configured": settings.aoai_configured,
        "analyzer_id": settings.cu_analyzer_id,
        "max_upload_mb": settings.max_upload_mb,
        "blob_upload_enabled": settings.blob_configured,
        "max_blob_upload_bytes": settings.max_blob_upload_bytes,
        "web_search_enabled": settings.web_search_enabled,
    }


@app.get("/api/platforms")
async def platforms() -> dict[str, str]:
    return {key: PLATFORM_NAMES.get(key, key) for key in sorted(ALLOWED_PLATFORMS)}


@app.post("/api/analyze", status_code=202)
async def analyze(
    background_tasks: BackgroundTasks,
    target_platform: Annotated[str, Form()] = "general",
    video_purpose: Annotated[VideoPurpose, Form()] = "General review",
    goal: Annotated[str, Form()] = "",
    use_web_search: Annotated[bool, Form()] = True,
    video_url: Annotated[str, Form()] = "",
    file: Annotated[UploadFile | None, File()] = None,
) -> dict[str, str]:
    settings = get_settings()
    if not settings.cu_configured:
        raise HTTPException(503, "Content Understanding is not configured. Set CU_ENDPOINT and CU_KEY in .env.")
    if not settings.aoai_configured:
        raise HTTPException(503, "Azure OpenAI is not configured. Set AOAI_ENDPOINT, AOAI_KEY and AOAI_DEPLOYMENT in .env.")

    if target_platform not in ALLOWED_PLATFORMS:
        raise HTTPException(400, f"Unsupported platform '{target_platform}'. Choose one of {sorted(ALLOWED_PLATFORMS)}.")

    video_url = video_url.strip()
    if not file and not video_url:
        raise HTTPException(400, "Provide either a video file or a video URL.")

    data: bytes | None = None
    content_type = "video/mp4"
    filename = ""

    if file is not None and file.filename:
        content_type = (file.content_type or "").lower()
        if content_type not in ALLOWED_VIDEO_TYPES:
            raise HTTPException(415, f"Unsupported content type '{content_type}'. Upload an mp4, mov, webm or mkv file.")
        data = await file.read(settings.max_upload_bytes + 1)
        if len(data) > settings.max_upload_bytes:
            raise HTTPException(413, f"File exceeds the {settings.max_upload_mb} MB limit.")
        if not data:
            raise HTTPException(400, "Uploaded file is empty.")
        filename = Path(file.filename).name
        video_url = ""
    else:
        try:
            parsed_url = urlsplit(video_url)
            hostname = (parsed_url.hostname or "").lower().rstrip(".")
        except ValueError:
            raise HTTPException(400, "Provide a valid direct video file URL.") from None
        if parsed_url.scheme not in {"http", "https"} or not hostname:
            raise HTTPException(400, "video_url must be an http(s) URL reachable by the Content Understanding service.")
        page_hosts = ("youtube.com", "youtu.be", "youtube-nocookie.com")
        if any(hostname == host or hostname.endswith("." + host) for host in page_hosts):
            raise HTTPException(
                400,
                "YouTube page links are not direct video files. Upload the video file or provide a direct downloadable video URL.",
            )

    job = store.create(
        filename=filename,
        source_url=video_url,
        target_platform=target_platform,  # type: ignore[arg-type]
        video_purpose=video_purpose,
        goal=goal.strip()[:500] or "General video review",
        use_web_search=use_web_search and settings.web_search_enabled,
    )
    background_tasks.add_task(
        _run, job.id, data=data, content_type=content_type, source_url=video_url
    )
    return {"job_id": job.id}


async def _run(job_id: str, *, data: bytes | None, content_type: str, source_url: str) -> None:
    await run_job(
        store,
        job_id,
        get_settings(),
        data=data,
        content_type=content_type,
        source_url=source_url,
    )


app.include_router(create_upload_router(lambda: store, lambda: get_settings(), lambda: uploads, _run, ALLOWED_VIDEO_TYPES))


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str, response: Response) -> Job:
    job = store.get(job_id)
    if job is None:
        raise HTTPException(404, "Job not found.")
    response.headers["Cache-Control"] = "no-store"
    return job


@app.delete("/api/jobs/{job_id}", status_code=204)
async def delete_job(job_id: str) -> Response:
    job = store.get(job_id)
    if job and job.blob_upload_id:
        if job.blob_upload_active or job.status not in {"succeeded", "failed"} or any(turn.status == "running" for turn in job.director_chat.turns):
            raise HTTPException(409, "Analysis or chat is still running. Wait before deleting the report.")
        session = uploads.sessions.get(job.blob_upload_id)
        if session:
            try:
                await uploads.cleanup(session, get_settings())
            except Exception as error:
                logger.warning("Report Blob cleanup failed (%s)", type(error).__name__)
                raise HTTPException(503, "Could not remove TakeTwo's staged Blob. The report is retained; retry deletion.") from None
    try:
        store.delete(job_id)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from None
    return Response(status_code=204, headers={"Cache-Control": "no-store"})


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")
