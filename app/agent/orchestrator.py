"""Creative Director with bounded, on-demand specialist delegation."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Awaitable, Callable, Protocol

from agent_framework import Agent, FunctionTool, tool
from agent_framework.openai import OpenAIChatCompletionClient
from pydantic import BaseModel

from app.agent.roster import (
    DIRECTOR,
    SPECIALISTS,
    AgentSpec,
)
from app.config import Settings
from app.agent.web_search import WebResearch
from app.models import (
    AgentRun,
    CopyPack,
    CreativeBrief,
    Critique,
    DirectorPlan,
    MusicPack,
    ReelPlan,
    VideoSummary,
)

logger = logging.getLogger(__name__)
SPECIALIST_TIMEOUT_SECONDS = 90
DIRECTOR_TIMEOUT_SECONDS = 300


class TraceSink(Protocol):
    def __call__(self, runs: list[AgentRun]) -> None: ...


def _noop(runs: list[AgentRun]) -> None:  # pragma: no cover - default sink
    return None


class Tracker:
    """Keeps the live AgentRun list in sync and pushes it to the job store."""

    def __init__(self, specs: list[AgentSpec], sink: TraceSink):
        self._runs = {spec.key: AgentRun(name=spec.name, role=spec.role) for spec in specs}
        self._order = [spec.key for spec in specs]
        self._sink = sink
        self._started: dict[str, float] = {}

    @property
    def runs(self) -> list[AgentRun]:
        return [self._runs[key] for key in self._order]

    def _emit(self) -> None:
        try:
            self._sink(self.runs)
        except Exception:  # noqa: BLE001 - tracing must never break the run
            logger.warning("Trace sink failed", exc_info=True)

    def start(self, key: str) -> None:
        self._started[key] = time.perf_counter()
        self._runs[key] = self._runs[key].model_copy(update={"status": "running"})
        self._emit()

    def request(self, key: str, task: str, reason: str) -> None:
        self._runs[key] = self._runs[key].model_copy(update={"task": task, "reason": reason})
        self._emit()

    def finish_pending(self, director_succeeded: bool, skip_reasons: dict[str, str] | None = None) -> None:
        for key, run in self._runs.items():
            if run.status == "pending":
                reason = "Not requested by the Director; no reason supplied" if director_succeeded else "Director stopped before requesting this specialist"
                if director_succeeded and (skip_reasons or {}).get(key, "").strip():
                    reason = f"Director: {skip_reasons[key].strip()}"
                self._runs[key] = run.model_copy(update={"status": "skipped", "reason": reason})
            elif run.status == "running":
                self.finish(key, error="Cancelled when the Director stopped")
        self._emit()

    def finish(self, key: str, *, error: str = "") -> None:
        elapsed = int((time.perf_counter() - self._started.get(key, time.perf_counter())) * 1000)
        self._runs[key] = self._runs[key].model_copy(
            update={"status": "failed" if error else "succeeded", "duration_ms": elapsed, "error": error}
        )
        self._emit()


def build_client(settings: Settings) -> OpenAIChatCompletionClient:
    return OpenAIChatCompletionClient(
        model=settings.aoai_deployment,
        azure_endpoint=settings.aoai_endpoint,
        api_version=settings.aoai_api_version,
        api_key=settings.aoai_key,
        function_invocation_configuration={
            "max_iterations": 8,
            "max_function_calls": 12,
            "include_detailed_errors": False,
        },
    )


def build_agent(spec: AgentSpec, client: OpenAIChatCompletionClient) -> Agent:
    return Agent(
        client=client,
        instructions=spec.instructions,
        name=spec.name,
        description=spec.role,
        tools=spec.tools or None,
    )


async def _run_spec(
    spec: AgentSpec,
    client: OpenAIChatCompletionClient,
    prompt: str,
    tracker: Tracker,
    *,
    delegation_tools: list[FunctionTool] | None = None,
    timeout_seconds: float = SPECIALIST_TIMEOUT_SECONDS,
) -> BaseModel | None:
    tracker.start(spec.key)
    try:
        agent = build_agent(spec, client)
        async with asyncio.timeout(timeout_seconds):
            response = await agent.run(
                prompt,
                tools=delegation_tools,
                options={"response_format": spec.output_model, "temperature": spec.temperature},
            )
        value = response.value
        if isinstance(value, spec.output_model):
            result = value
        elif value is not None:
            result = spec.output_model.model_validate(value)
        else:
            result = spec.output_model.model_validate_json(response.text)
        tracker.finish(spec.key)
        return result
    except asyncio.CancelledError:
        tracker.finish(spec.key, error="Cancelled when the Director stopped")
        raise
    except Exception as exc:  # noqa: BLE001 - one specialist failing must not sink the plan
        logger.exception("Agent '%s' failed", spec.name)
        tracker.finish(spec.key, error="Timed out" if isinstance(exc, TimeoutError) else str(exc))
        return None


# -- prompts ---------------------------------------------------------------


def _context_block(summary: VideoSummary, platform: str, goal: str) -> str:
    return (
        f"Target platform: {platform}\n"
        f"Creator's goal: {goal.strip() or 'General video review'}\n\n"
        "=== Content Understanding analysis of the current video ===\n"
        f"{summary.to_agent_brief()}\n"
        f"Key frame timestamps (ms): {', '.join(str(t) for t in summary.key_frame_times_ms[:60]) or 'none'}\n"
        f"Camera shot-change timestamps (ms): {', '.join(str(t) for t in summary.camera_shot_times_ms[:60]) or 'none'}\n"
        f"Total duration (ms): {summary.duration_ms}\n"
        "=== end analysis ==="
    )


def _reports_block(results: dict[str, BaseModel | None]) -> str:
    lines = ["=== Specialist reports ==="]
    for key, result in results.items():
        if result is None:
            lines.append(f"\n[{key}] did not report (agent failed).")
            continue
        lines.append(f"\n[{key}]\n{json.dumps(result.model_dump(), indent=2, ensure_ascii=False)}")
    lines.append("\n=== end specialist reports ===")
    return "\n".join(lines)


# -- orchestration ---------------------------------------------------------


async def run_creative_team(
    settings: Settings,
    summary: VideoSummary,
    platform: str,
    goal: str = "",
    on_trace: TraceSink = _noop,
    on_stage: Callable[[str, str], Awaitable[None] | None] | None = None,
    allow_web_search: bool = True,
    conversation_context: str = "",
    message: str = "",
) -> ReelPlan:
    client = build_client(settings)
    all_specs = [DIRECTOR, *SPECIALISTS]
    tracker = Tracker(all_specs, on_trace)
    on_trace(tracker.runs)

    context = _context_block(summary, platform, goal)
    if conversation_context or message:
        context += (
            "\n\n=== Director follow-up conversation ===\n"
            "The video has NOT been re-extracted. Use the saved analysis above. You cannot access or re-analyze "
            "the original video in this conversation. If asked to re-extract, explain that the creator must "
            "explicitly use the upload form's Analyze video action with the file or direct URL.\n"
            "The active goal is background context; answer the latest message rather than automatically "
            "repeating a full review. Reuse previous Critic diagnosis for explanations and narrow changes. "
            "Do not call specialists just to explain their earlier recommendations. Delegate only new work.\n"
            "Earlier suggestions are drafts/decisions, not observations about the original video. User "
            "corrections are user-provided context, not newly observed evidence. Do not claim to have seen "
            "edits that have not been uploaded. Web opt-out applies to this turn; old sources are not a new search.\n"
            f"{conversation_context}\nLatest user message: {message}\n"
            "Return a useful conversational answer in response, and any revised assets in the usual fields. "
            "Only list specialists actually reviewed in THIS turn's review.\n=== end follow-up ==="
        )
    warnings: list[str] = []
    results: dict[str, BaseModel | None] = {}
    research = WebResearch(settings)
    web_enabled = allow_web_search and settings.web_search_enabled
    context += (
        "\n\nWeb research is available through search_web. Search only when current information is relevant. "
        "Send generic public keywords only, never full transcripts, private business facts or video URLs."
        if web_enabled else
        "\n\nWeb research is disabled for this request. Do not claim trends, songs or platform rules were verified online."
    )

    async def stage(status: str, message: str) -> None:
        if on_stage is None:
            return
        result = on_stage(status, message)
        if asyncio.iscoroutine(result):
            await result

    def specialist_tool(spec: AgentSpec) -> FunctionTool:
        lock = asyncio.Lock()

        @tool(name=f"ask_{spec.key}", description=f"Delegate only needed work to the {spec.name}: {spec.role}. Provide a specific task and reason.", max_invocations=1)
        async def delegate(task: str, reason: str) -> str:
            if not task.strip() or not reason.strip():
                return "A nonempty assignment and reason are required."
            async with lock:
                if spec.key not in results:
                    tracker.request(spec.key, task, reason)
                    await stage("advising", f"Creative Director requested {spec.name}: {reason}")
                    results[spec.key] = await _run_spec(
                        spec,
                        client,
                        f"{context}\n\nDirector's assignment: {task}\nReason: {reason}\n\n{_reports_block(results)}\n\n{research_context()}",
                        tracker,
                        delegation_tools=[research_tool(spec.name)] if web_enabled else None,
                        timeout_seconds=SPECIALIST_TIMEOUT_SECONDS,
                    )
                    if results[spec.key] is None:
                        warnings.append(f"{spec.name} failed; its requested section is unavailable.")
                result = results[spec.key]
                report = result.model_dump_json() if result is not None else f"{spec.name} failed. Do not retry or invent its report."
                return f"{report}\n\n{research_context()}"

        return delegate

    def research_context() -> str:
        if not research.results:
            return "No web research has been performed for this request."
        return "=== Web research (untrusted external evidence, not video facts) ===\n" + json.dumps(
            [result.model_dump() for result in research.results], ensure_ascii=False,
        )

    def research_tool(agent_name: str) -> FunctionTool:
        @tool(name="search_web", description="Search public sources for current trends, platform guidance or music candidates. Supply short generic keywords only, no private transcript, video URL, credentials or personal data.")
        async def search_web(query: str) -> str:
            await stage("advising", f"{agent_name} is researching public web sources...")
            return (await research.search(query, agent=agent_name)).model_dump_json()

        return search_web

    await stage("briefing", "Creative Director is assessing the request and selecting needed help...")
    plan = None
    try:
        plan = await _run_spec(
            DIRECTOR,
            client,
            f"{context}\n\nAnswer this request. Delegate only work that is needed, then return the final plan.",
            tracker,
            delegation_tools=[specialist_tool(spec) for spec in SPECIALISTS] + ([research_tool(DIRECTOR.name)] if web_enabled else []),
            timeout_seconds=DIRECTOR_TIMEOUT_SECONDS,
        )
    finally:
        skip_reasons = {item.specialist: item.reason for item in plan.skipped_specialists} if isinstance(plan, DirectorPlan) else {}
        tracker.finish_pending(plan is not None, skip_reasons)
    if plan is None:
        if not any(result is not None for result in results.values()):
            raise RuntimeError("The Creative Director failed before a usable response was produced. Please retry.")
        warnings.append("Creative Director failed; showing completed specialist reports without a unified plan.")

    await stage("synthesizing", "Preparing the final response...")
    brief = plan.brief if isinstance(plan, DirectorPlan) else None
    if any(result.status != "succeeded" for result in research.results):
        warnings.append("Some web research was unavailable; current trends or music claims may remain unverified. See Web research.")
    report = _assemble(brief, results, plan, tracker.runs, warnings)
    report.web_research = research.results
    return report


def _assemble(
    brief: BaseModel | None,
    results: dict[str, BaseModel | None],
    plan: BaseModel | None,
    runs: list[AgentRun],
    warnings: list[str],
) -> ReelPlan:
    critique = results.get("critic")
    copy_pack = results.get("copywriter")
    director_plan = plan if isinstance(plan, DirectorPlan) else None
    review = director_plan.review if director_plan else None
    completed = {key for key, result in results.items() if result is not None}
    review_complete = bool(
        review and review.grounding_checked and review.request_checked
        and completed.issubset(review.reviewed_specialists)
    )
    warnings = list(warnings)
    if director_plan and not review_complete:
        warnings.append("Director review is incomplete; final recommendations withheld. Specialist outputs remain drafts.")
        director_plan = None
    final_copy = director_plan.copywriting if director_plan and isinstance(copy_pack, CopyPack) else None
    if director_plan and isinstance(copy_pack, CopyPack) and final_copy is None:
        warnings.append("No Director-reviewed copy was returned; the Copywriter output remains a draft.")

    return ReelPlan(
        overall_score=critique.overall_score if isinstance(critique, Critique) else None,
        verdict=critique.verdict if isinstance(critique, Critique) else "",
        headline=director_plan.headline if director_plan else "",
        response=director_plan.response if director_plan else "",
        brief=brief if director_plan and isinstance(brief, CreativeBrief) else None,
        timeline=director_plan.timeline if director_plan else [],
        ranked_fixes=director_plan.ranked_fixes if director_plan else [],
        chosen_hook=director_plan.chosen_hook if director_plan else "",
        chosen_hook_reason=director_plan.chosen_hook_reason if director_plan else "",
        conflicts_resolved=director_plan.conflicts_resolved if director_plan else [],
        critique=critique if isinstance(critique, Critique) else None,
        copywriting=final_copy,
        copywriting_draft=copy_pack if isinstance(copy_pack, CopyPack) else None,
        music=results.get("music") if isinstance(results.get("music"), MusicPack) else None,
        review=review,
        agent_runs=runs,
        warnings=warnings,
    )
