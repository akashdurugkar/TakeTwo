"""In-memory job store. Swap for Redis or Cosmos DB when running more than one worker."""

from __future__ import annotations

import threading
import uuid
from collections import OrderedDict
from typing import Any

from app.models import DirectorChatTurn, DirectorConversation, Job, Platform

MAX_JOBS = 100


class JobStore:
    def __init__(self, max_jobs: int = MAX_JOBS):
        self._jobs: OrderedDict[str, Job] = OrderedDict()
        self._lock = threading.Lock()
        self._max_jobs = max_jobs

    def create(self, *, filename: str = "", source_url: str = "", target_platform: Platform, goal: str, use_web_search: bool = True, blob_upload_id: str = "") -> Job:
        job = Job(
            id=uuid.uuid4().hex,
            filename=filename,
            source_url=source_url,
            blob_upload_id=blob_upload_id,
            blob_upload_active=bool(blob_upload_id),
            target_platform=target_platform,
            goal=goal,
            use_web_search=use_web_search,
            stage_message="Queued.",
        )
        with self._lock:
            self._jobs[job.id] = job
            while len(self._jobs) > self._max_jobs:
                evictable = next((key for key, value in self._jobs.items() if key != job.id and value.status in {"succeeded", "failed"} and not value.blob_upload_active and not value.blob_cleanup_pending and not any(
                    turn.status == "running" for turn in value.director_chat.turns
                )), None)
                if evictable is None:
                    break
                del self._jobs[evictable]
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def update(self, job_id: str, **changes: Any) -> Job | None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return None
            updated = job.model_copy(update=changes)
            self._jobs[job_id] = updated
            return updated

    def list(self) -> list[Job]:
        with self._lock:
            return list(reversed(self._jobs.values()))

    def delete(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return False
            if job.status not in {"succeeded", "failed"} or job.blob_upload_active:
                raise ValueError("Analysis is still running. Wait for it to finish before deleting the report.")
            if any(turn.status == "running" for turn in job.director_chat.turns):
                raise ValueError("Director chat is still running. Wait for it to finish before deleting the report.")
            del self._jobs[job_id]
            return True

    def begin_chat(self, job_id: str, turn: DirectorChatTurn) -> tuple[Job, bool]:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise KeyError(job_id)
            if job.status not in {"succeeded", "failed"} or job.blob_upload_active or job.video_summary is None:
                raise ValueError("Wait for the current analysis to finish; saved extraction is required for chat.")
            for existing in job.director_chat.turns:
                if existing.id == turn.id:
                    if (existing.message, existing.goal, existing.use_web_search) != (turn.message, turn.goal, turn.use_web_search):
                        raise ValueError("This request ID was already used for a different message.")
                    return job.model_copy(deep=True), False
            if any(existing.status == "running" for existing in job.director_chat.turns):
                raise ValueError("The Director is already responding. Wait before sending another message.")
            if len(job.director_chat.turns) >= 20:
                raise ValueError("This conversation has reached its 20-turn limit. Existing messages and reports are still available.")
            conversation = DirectorConversation(
                goal=job.director_chat.goal or job.goal or "General video review",
                turns=[*job.director_chat.turns, turn],
            )
            updated = job.model_copy(update={"director_chat": conversation})
            self._jobs[job_id] = updated
            return updated.model_copy(deep=True), True

    def update_chat_turn(self, job_id: str, turn_id: str, **changes: Any) -> None:
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                return
            turns = list(job.director_chat.turns)
            for index, turn in enumerate(turns):
                if turn.id != turn_id or turn.status != "running":
                    continue
                turns[index] = turn.model_copy(update=changes)
                goal = turn.goal if changes.get("status") == "succeeded" else job.director_chat.goal
                self._jobs[job_id] = job.model_copy(update={
                    "director_chat": DirectorConversation(goal=goal, turns=turns),
                })
                return
