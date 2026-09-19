"""Director conversations over a job's saved extraction; no Content Understanding calls."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable
from uuid import UUID, uuid4

from fastapi import APIRouter, BackgroundTasks, HTTPException, Response
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.agent.orchestrator import run_creative_team
from app.config import Settings
from app.jobs import JobStore
from app.models import DirectorChatTurn, DirectorConversation, Job

MAX_CONTEXT_CHARACTERS = 160_000


class ChatContextLimit(ValueError):
    pass


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: UUID = Field(default_factory=uuid4)
    message: str = Field("", max_length=2000)
    goal: str | None = Field(None, max_length=500)
    use_web_search: bool = False

    @field_validator("message", "goal")
    @classmethod
    def trim_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @model_validator(mode="after")
    def require_message_or_goal(self) -> "ChatRequest":
        if self.goal == "":
            raise ValueError("A changed goal cannot be blank.")
        if not self.message and not self.goal:
            raise ValueError("Enter a message or a new goal.")
        return self


def conversation_context(job: Job, current: DirectorChatTurn) -> str:
    prior = [turn for turn in job.director_chat.turns if turn.id != current.id and turn.status == "succeeded"]
    context = {
        "original_goal": job.goal,
        "active_goal": current.goal,
        "original_report": job.report.model_dump(exclude={"agent_runs", "copywriting_draft"}) if job.report else None,
        "conversation": [{
            "user": turn.message, "goal": turn.goal, "director": turn.answer,
            "recommendations": turn.report.model_dump(exclude={"agent_runs", "copywriting_draft"}) if turn.report else None,
        } for turn in prior],
    }
    text = json.dumps(context, ensure_ascii=False)
    if len(text) > MAX_CONTEXT_CHARACTERS:
        raise ChatContextLimit("Conversation context limit reached. Existing replies and extraction are retained; no new analysis was started.")
    return text


async def respond(store: JobStore, job: Job, turn: DirectorChatTurn, settings: Settings) -> None:
    try:
        if job.video_summary is None:
            raise ValueError("No saved extraction")
        report = await run_creative_team(
            settings,
            job.video_summary,
            job.target_platform,
            turn.goal,
            message=turn.message,
            conversation_context=conversation_context(job, turn),
            allow_web_search=turn.use_web_search,
            on_trace=lambda runs: store.update_chat_turn(job.id, turn.id, agent_runs=runs),
            on_stage=lambda status, text: store.update_chat_turn(job.id, turn.id, stage_message=text),
        )
        answer = report.response or report.headline
        if not answer:
            raise ValueError("No Director-reviewed response was returned.")
        store.update_chat_turn(
            job.id, turn.id, status="succeeded", answer=answer, report=report, stage_message="Done.",
        )
    except asyncio.CancelledError:
        store.update_chat_turn(job.id, turn.id, status="failed", error="Director response was cancelled. Saved extraction is unchanged.", stage_message="Cancelled.")
        raise
    except ChatContextLimit as exc:
        store.update_chat_turn(job.id, turn.id, status="failed", error=str(exc), stage_message="Context limit reached.")
    except Exception:
        store.update_chat_turn(
            job.id, turn.id, status="failed", stage_message="Failed.",
            error="The Director could not complete this reply. Retry with the saved extraction; no video was re-extracted.",
        )


def create_router(get_store: Callable[[], JobStore], get_settings: Callable[[], Settings]) -> APIRouter:
    router = APIRouter(prefix="/api/jobs/{job_id}/chat", tags=["Director chat"])

    @router.get("", response_model=DirectorConversation)
    async def get_conversation(job_id: str, response: Response) -> DirectorConversation:
        job = get_store().get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found. Saved extraction and chat expire on server restart or deletion.")
        response.headers["Cache-Control"] = "no-store"
        return job.director_chat.model_copy(update={"goal": job.director_chat.goal or job.goal or "General video review"})

    @router.post("", status_code=202)
    async def post_message(job_id: str, request: ChatRequest, background_tasks: BackgroundTasks, response: Response) -> dict[str, str]:
        store = get_store()
        job = store.get(job_id)
        if job is None:
            raise HTTPException(404, "Job not found. Upload explicitly to create a new extraction.")
        settings = get_settings()
        if not settings.aoai_configured:
            raise HTTPException(503, "Azure OpenAI is not configured for Director chat.")
        turn = DirectorChatTurn(
            id=str(request.request_id),
            message=request.message or "Update the recommendations for the new goal using the saved extraction.",
            goal=request.goal or job.director_chat.goal or job.goal or "General video review",
            use_web_search=request.use_web_search and settings.web_search_enabled,
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        try:
            conversation_context(job, turn)
        except ChatContextLimit as exc:
            raise HTTPException(409, str(exc)) from None
        try:
            snapshot, created = store.begin_chat(job_id, turn)
        except KeyError:
            raise HTTPException(404, "Job not found.") from None
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if created:
            background_tasks.add_task(respond, store, snapshot, turn, settings)
        response.headers["Cache-Control"] = "no-store"
        return {"job_id": job_id, "turn_id": turn.id}

    return router