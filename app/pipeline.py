"""TakeTwo pipeline: video -> Content Understanding -> VideoSummary -> creative team -> report."""

from __future__ import annotations

import logging

from app.agent.orchestrator import run_creative_team
from app.config import Settings
from app.cu.analyzer_schema import reel_analyzer_definition
from app.cu.client import ContentUnderstandingClient, ContentUnderstandingError, safe_error_detail
from app.cu.mapper import to_video_summary
from app.jobs import JobStore
from app.models import AgentRun, VideoSummary

logger = logging.getLogger(__name__)


async def resolve_analyzer(client: ContentUnderstandingClient, settings: Settings) -> str:
    """Ensure the custom video analyzer exists, degrading to a prebuilt analyzer if it cannot be created."""
    for base_id in (settings.cu_base_analyzer_id, settings.cu_fallback_base_analyzer_id):
        if not base_id:
            continue
        try:
            analyzer = await client.ensure_analyzer(
                settings.cu_analyzer_id, reel_analyzer_definition(base_id)
            )
        except ContentUnderstandingError as exc:
            logger.warning("Could not create analyzer from base '%s': %s", base_id, exc)
            continue
        if analyzer.get("status") == "ready":
            return settings.cu_analyzer_id
        logger.warning("Analyzer '%s' is in status '%s'.", settings.cu_analyzer_id, analyzer.get("status"))

    logger.warning(
        "Falling back to prebuilt analyzer '%s'; custom video fields may be unavailable.",
        settings.cu_prebuilt_analyzer_id,
    )
    return settings.cu_prebuilt_analyzer_id


async def analyze_video(
    settings: Settings,
    *,
    data: bytes | None = None,
    content_type: str = "video/mp4",
    source_url: str = "",
) -> tuple[VideoSummary, str]:
    async with ContentUnderstandingClient(
        settings.cu_endpoint, settings.cu_key, settings.cu_api_version,
        analysis_timeout=settings.cu_analysis_timeout_seconds,
    ) as client:
        analyzer_id = await resolve_analyzer(client, settings)
        if source_url:
            result = await client.analyze_url(analyzer_id, source_url)
        elif data is not None:
            result = await client.analyze_bytes(analyzer_id, data, content_type)
        else:
            raise ValueError("Either video bytes or a source URL must be provided.")
    return to_video_summary(result), analyzer_id


async def run_job(
    store: JobStore,
    job_id: str,
    settings: Settings,
    *,
    data: bytes | None = None,
    content_type: str = "video/mp4",
    source_url: str = "",
) -> None:
    job = store.get(job_id)
    if job is None:
        return

    try:
        store.update(
            job_id,
            status="analyzing",
            stage_message="Extracting scenes, transcript and video signals with Content Understanding...",
        )
        summary, analyzer_id = await analyze_video(
            settings, data=data, content_type=content_type, source_url=source_url
        )
        store.update(
            job_id,
            status="advising",
            analyzer_id=analyzer_id,
            video_summary=summary,
            stage_message="Assembling the creative team…",
        )

        def on_trace(runs: list[AgentRun]) -> None:
            store.update(job_id, agent_runs=runs)

        def on_stage(status: str, message: str) -> None:
            store.update(job_id, status=status, stage_message=message)

        plan = await run_creative_team(
            settings,
            summary,
            job.target_platform,
            job.goal,
            on_trace=on_trace,
            on_stage=on_stage,
            allow_web_search=job.use_web_search,
        )
        store.update(job_id, status="succeeded", report=plan, stage_message="Done.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the client as job state
        if job.blob_upload_id:
            if isinstance(exc, ContentUnderstandingError):
                detail = safe_error_detail(exc, secrets=(source_url, settings.cu_key, settings.aoai_key))
                message = f"Content Understanding failed: {detail}"
                if "InvalidImageDimension" in detail:
                    message += " Resize the video to supported dimensions, for example 1920x1080 for landscape or 1080x1920 for portrait. Reducing file size alone will not fix this."
            else:
                current = store.get(job_id)
                stage = "Creative recommendations" if current and current.video_summary else "Video extraction"
                message = f"{stage} failed ({type(exc).__name__})."
            logger.error("Staged job %s: %s", job_id, message)
        else:
            logger.exception("Job %s failed", job_id)
            message = str(exc)
        store.update(job_id, status="failed", error=message, stage_message="Failed.")
