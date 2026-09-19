import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent_framework import ChatResponse, Content, Message

from app.agent.orchestrator import _assemble, _context_block, build_agent, build_client, run_creative_team
from app.config import Settings
from app.agent.web_search import parse_research
from app.models import CaptionSuggestion, CopyPack, CreativeBrief, Critique, DirectorPlan, DirectorReview, HookSuggestion, MusicPack, PlanBeat, SkippedSpecialist, VideoSummary


class EditPlanContractTests(unittest.TestCase):
    def test_original_and_proposed_positions_are_separate(self):
        beat = PlanBeat(start="0:00", end="0:00.5", source_start_ms=8500, source_end_ms=9000,
                        action="move", timing="normal", say="-", show="Side-profile shot", overlay="Travel", why="Open on a person")
        restored = DirectorPlan.model_validate_json(DirectorPlan(headline="Move the side-profile shot first", timeline=[beat]).model_dump_json()).timeline[0]
        self.assertEqual(restored.start, "0:00")
        self.assertEqual(restored.source_start_ms, 8500)
        self.assertEqual(restored.source_end_ms, 9000)
        self.assertEqual(restored.action, "move")
        self.assertEqual(restored.timing, "normal")

    def test_legacy_beats_remain_unknown_instead_of_inventing_source_timestamps(self):
        beat = PlanBeat(start="0:00", end="0:03", say="-", show="Windy shot (0.5-1.0s)", overlay="POV", why="Hook")
        self.assertIsNone(beat.source_start_ms)
        self.assertIsNone(beat.source_end_ms)
        self.assertEqual(beat.action, "unspecified")
        self.assertEqual(beat.timing, "unspecified")

    def test_duration_treatment_is_explicit_and_new_shots_have_no_source(self):
        beat = PlanBeat(start="0:00", end="0:03", source_start_ms=500, source_end_ms=1000,
                        action="trim", timing="freeze_frame", timing_note="Play 0.5s, then hold the last frame for 2.5s.",
                        say="-", show="Windy railing shot", overlay="POV", why="Opening")
        self.assertEqual(beat.timing, "freeze_frame")
        self.assertIn("2.5s", beat.timing_note)
        replacement = PlanBeat(start="0:00", end="0:03", action="new_shot", timing="normal", timing_note="Record a 3s close-up.", say="-", show="New close-up", overlay="-", why="Detail")
        self.assertIsNone(replacement.source_start_ms)


class DelegationTests(unittest.IsolatedAsyncioTestCase):
    def test_purpose_and_neutral_platform_context_are_distinct(self):
        context = _context_block(VideoSummary(summary="Observed demonstration"), "general", "Explain clearly", "Product demo")
        self.assertIn("No specific platform / Other", context)
        self.assertIn("Do not assume a social feed", context)
        self.assertIn('Video purpose (creator-provided context, not evidence): "Product demo"', context)
        self.assertIn("Creator's goal: Explain clearly", context)
        self.assertIn("Observed demonstration", context)

    def test_empty_goal_uses_general_review_not_growth_objective(self):
        for goal in ("", "   "):
            context = _context_block(VideoSummary(), "youtube_shorts", goal)
            self.assertIn("Creator's goal: General video review", context)
            self.assertNotIn("maximise reach", context)
        self.assertIn("Creator's goal: Write a caption", _context_block(VideoSummary(), "youtube_shorts", " Write a caption "))

    async def test_director_can_answer_without_specialists(self):
        created = []

        def build_agent(spec, client):
            created.append(spec.key)

            async def run(prompt, **kwargs):
                outputs = {
                    "director_brief": CreativeBrief(
                        angle="Keep the demonstration", target_emotion="Clarity", core_promise="A useful tip"
                    ),
                    "critic": Critique(overall_score=70, verdict="Clear demonstration"),
                    "copywriter": CopyPack(),
                    "music": MusicPack(),
                }
                return SimpleNamespace(
                    value=outputs.get(spec.key, DirectorPlan(
                        headline="Keep the demonstration",
                        review=DirectorReview(grounding_checked=True, request_checked=True, reviewed_specialists=[]),
                    ))
                )

            return SimpleNamespace(run=run)

        with patch("app.agent.orchestrator.build_client", return_value=object()), patch(
            "app.agent.orchestrator.build_agent", side_effect=build_agent
        ):
            report = await run_creative_team(
                Settings(_env_file=None), VideoSummary(summary="A useful tip"), "instagram_reels", "Summarize this video"
            )

        self.assertEqual(created, ["director"])
        self.assertIsNone(report.overall_score)
        self.assertIsNone(report.critique)
        self.assertEqual([run.status for run in report.agent_runs], ["succeeded", "skipped", "skipped", "skipped"])
        self.assertEqual(report.warnings, [])
        self.assertEqual(report.agent_runs[1].reason, "Not requested by the Director; no reason supplied")

    async def run_script(self, script, *, goal="Improve this reel", allow_web_search=True, conversation_context="", message=""):
        settings = Settings(
            _env_file=None,
            aoai_endpoint="https://example.invalid",
            aoai_key="test-only",
            aoai_deployment="test-model",
        )
        client = build_client(settings)
        requests = []
        traces = []

        async def respond(**kwargs):
            requests.append(kwargs)
            self.assertTrue(script, "Unexpected model request")
            response = script.pop(0)
            if isinstance(response, Exception):
                raise response
            if callable(response):
                return await response()
            return response

        with patch("app.agent.orchestrator.build_client", return_value=client), patch.object(
            client, "_inner_get_response", side_effect=respond
        ), patch("app.agent.orchestrator.build_agent", wraps=build_agent) as factory:
            report = await run_creative_team(
                settings,
                VideoSummary(summary="A practical demonstration", duration_ms=15000),
                "instagram_reels",
                goal,
                on_trace=lambda runs: traces.append([run.model_copy() for run in runs]),
                allow_web_search=allow_web_search,
                conversation_context=conversation_context,
                message=message,
            )
        self.assertEqual(script, [])
        return report, [call.args[0].key for call in factory.call_args_list], requests, traces

    def answer(self, model):
        return ChatResponse(messages=Message("assistant", [model.model_dump_json()]))

    async def test_follow_up_uses_history_and_returns_reviewed_answer(self):
        report, created, requests, _ = await self.run_script([
            self.answer(DirectorPlan(headline="Opening explanation", response="The earlier opening lacked context.", review=DirectorReview(
                grounding_checked=True, request_checked=True, reviewed_specialists=[],
            ))),
        ], goal="Education", conversation_context='{"previous_caption":"Show the fabric detail"}', message="Why that caption?")
        self.assertEqual(created, ["director"])
        self.assertEqual(report.response, "The earlier opening lacked context.")
        prompt = self.request_text(requests[0])
        self.assertIn("Show the fabric detail", prompt)
        self.assertIn("Latest user message: Why that caption?", prompt)
        self.assertIn("video has NOT been re-extracted", prompt)
        tools = [tool.name for tool in requests[0]["options"].get("tools", [])]
        self.assertFalse(any("extract" in name or "analyze" in name for name in tools))

    def request(self, name, task="Write two captions", reason="The user requested captions", call_id="call-1"):
        return ChatResponse(messages=Message("assistant", [Content.from_function_call(
            call_id, name, arguments={"task": task, "reason": reason}
        )]))

    def request_text(self, request):
        return "\n".join(
            message.text + " ".join(str(getattr(content, "result", "")) for content in message.contents)
            for message in request["messages"]
        )

    async def test_framework_invokes_only_copywriter(self):
        copy = CopyPack(captions=[CaptionSuggestion(text="A tip worth keeping")])
        report, created, requests, traces = await self.run_script([
            self.request("ask_copywriter"),
            self.answer(copy),
            self.answer(DirectorPlan(headline="Use the shorter caption")),
        ], goal="Rewrite my caption")

        self.assertEqual(created, ["director", "copywriter"])
        self.assertEqual(report.copywriting_draft, copy)
        self.assertIsNone(report.copywriting)
        self.assertIsNone(report.overall_score)
        self.assertIsNone(report.music)
        self.assertEqual([run.status for run in report.agent_runs], ["succeeded", "skipped", "succeeded", "skipped"])
        self.assertEqual(report.agent_runs[2].reason, "The user requested captions")
        self.assertTrue(any(runs[2].status == "running" for runs in traces))
        self.assertIn("Write two captions", self.request_text(requests[1]))
        self.assertFalse(any(tool.name.startswith("ask_") for tool in requests[1]["options"].get("tools", [])))
        self.assertIn("A tip worth keeping", self.request_text(requests[2]))

    async def test_dependent_assignment_receives_completed_report(self):
        critique = Critique(overall_score=0, verdict="The first five seconds lack context")
        report, created, requests, _ = await self.run_script([
            self.request("ask_critic", "Diagnose the opening", "Identify retention issues"),
            self.answer(critique),
            self.request("ask_copywriter", "Write a hook addressing the opening diagnosis", "Act on the Critic's finding", "call-2"),
            self.answer(CopyPack()),
            self.answer(DirectorPlan(headline="Open with the result")),
        ])

        self.assertEqual(created, ["director", "critic", "copywriter"])
        self.assertEqual(report.overall_score, 0)
        self.assertIn(critique.verdict, self.request_text(requests[3]))
        self.assertIsNone(report.music)

    async def test_duplicate_delegation_does_not_create_another_agent(self):
        report, created, _, _ = await self.run_script([
            self.request("ask_copywriter"),
            self.answer(CopyPack()),
            self.request("ask_copywriter", call_id="call-2"),
            self.answer(DirectorPlan(headline="Caption ready")),
        ])
        self.assertEqual(created, ["director", "copywriter"])
        self.assertIsNotNone(report.copywriting_draft)

    async def test_specialist_failure_is_not_reported_as_skipped(self):
        with self.assertLogs("app.agent.orchestrator", level="ERROR"):
            report, created, _, _ = await self.run_script([
                self.request("ask_music", "Suggest a sonic direction", "User requested music"),
                RuntimeError("Simulated model outage"),
                self.answer(DirectorPlan(headline="Music advice is unavailable", review=DirectorReview(
                    grounding_checked=True, request_checked=True, reviewed_specialists=[],
                ))),
            ])
        self.assertEqual(created, ["director", "music"])
        self.assertEqual(report.agent_runs[3].status, "failed")
        self.assertIsNone(report.music)
        self.assertEqual(len(report.warnings), 1)
        self.assertIn("Music Curator", report.warnings[0])

    async def test_director_failure_without_results_fails_request(self):
        with self.assertLogs("app.agent.orchestrator", level="ERROR"), self.assertRaisesRegex(
            RuntimeError, "before a usable response"
        ):
            await self.run_script([RuntimeError("Simulated model outage")])

    async def test_director_failure_preserves_completed_specialist(self):
        with self.assertLogs("app.agent.orchestrator", level="ERROR"):
            report, _, _, _ = await self.run_script([
                self.request("ask_copywriter"),
                self.answer(CopyPack()),
                RuntimeError("Simulated synthesis outage"),
            ])
        self.assertIsNotNone(report.copywriting_draft)
        self.assertIsNone(report.copywriting)
        self.assertEqual(report.agent_runs[0].status, "failed")
        self.assertTrue(all(run.status not in ("pending", "running") for run in report.agent_runs))
        self.assertIn("without a unified plan", report.warnings[0])

    async def test_specialist_timeout_returns_partial_response(self):
        async def blocked_response():
            await asyncio.Event().wait()

        with patch("app.agent.orchestrator.SPECIALIST_TIMEOUT_SECONDS", 0.01), self.assertLogs(
            "app.agent.orchestrator", level="ERROR"
        ):
            report, _, _, _ = await self.run_script([
                self.request("ask_copywriter"),
                blocked_response,
                self.answer(DirectorPlan(headline="Caption generation timed out")),
            ])
        self.assertEqual(report.agent_runs[2].status, "failed")
        self.assertEqual(report.agent_runs[2].error, "Timed out")
        self.assertIsNone(report.copywriting)

    async def test_director_timeout_cancels_active_specialist(self):
        async def blocked_response():
            await asyncio.Event().wait()

        with patch("app.agent.orchestrator.DIRECTOR_TIMEOUT_SECONDS", 0.02), self.assertLogs(
            "app.agent.orchestrator", level="ERROR"
        ), self.assertRaisesRegex(RuntimeError, "before a usable response"):
            await self.run_script([self.request("ask_copywriter"), blocked_response])

    async def test_reviewed_copy_and_skip_reasons_come_from_director(self):
        draft = CopyPack(captions=[CaptionSuggestion(text="Guaranteed delivery tomorrow")])
        corrected = CopyPack(captions=[CaptionSuggestion(text="Explore the fabric collection")])
        report, created, _, _ = await self.run_script([
            self.request("ask_copywriter"),
            self.answer(draft),
            self.answer(DirectorPlan(
                headline="Focus on the fabric variety", copywriting=corrected,
                review=DirectorReview(
                    grounding_checked=True, request_checked=True, reviewed_specialists=["copywriter"],
                    notes=["Removed the unsupported delivery promise"],
                    claims_to_confirm=["What delivery times can you actually offer?"],
                ),
                skipped_specialists=[
                    SkippedSpecialist(specialist="critic", reason="Caption-only request; no overall assessment requested"),
                    SkippedSpecialist(specialist="music", reason="No audio changes requested"),
                    SkippedSpecialist(specialist="copywriter", reason="Incorrect skip entry must not alter execution"),
                ],
            )),
        ], goal="Write a caption")
        self.assertEqual(created, ["director", "copywriter"])
        self.assertEqual(report.copywriting, corrected)
        self.assertEqual(report.copywriting_draft, draft)
        self.assertEqual(report.agent_runs[1].reason, "Director: Caption-only request; no overall assessment requested")
        self.assertEqual(report.agent_runs[2].status, "succeeded")
        self.assertEqual(report.agent_runs[2].reason, "The user requested captions")
        self.assertEqual(report.review.claims_to_confirm, ["What delivery times can you actually offer?"])
        self.assertFalse(report.warnings)

    def test_missing_or_incomplete_review_never_promotes_raw_hook(self):
        draft = CopyPack(better_hooks=[HookSuggestion(text="Guaranteed quality", rationale="Draft", delivery="Overlay")])
        for review in (
            None,
            DirectorReview(grounding_checked=False, request_checked=True, reviewed_specialists=["copywriter"]),
            DirectorReview(grounding_checked=True, request_checked=False, reviewed_specialists=["copywriter"]),
            DirectorReview(grounding_checked=True, request_checked=True, reviewed_specialists=[]),
        ):
            with self.subTest(review=review):
                plan = DirectorPlan(headline="Unreviewed headline", chosen_hook="Guaranteed quality", copywriting=draft, review=review)
                report = _assemble(None, {"copywriter": draft}, plan, [], [])
                self.assertEqual(report.headline, "")
                self.assertEqual(report.chosen_hook, "")
                self.assertIsNone(report.copywriting)
                self.assertEqual(report.copywriting_draft, draft)
                self.assertIn("review is incomplete", report.warnings[0])
        report = _assemble(None, {"copywriter": draft}, None, [], [])
        self.assertEqual(report.chosen_hook, "")
        self.assertIsNone(report.copywriting)

    def test_reviewed_draft_without_final_copy_is_not_promoted(self):
        draft = CopyPack(captions=[CaptionSuggestion(text="Original draft")])
        plan = DirectorPlan(headline="A direction", review=DirectorReview(
            grounding_checked=True, request_checked=True, reviewed_specialists=["copywriter"],
        ))
        report = _assemble(None, {"copywriter": draft}, plan, [], [])
        self.assertIsNone(report.copywriting)
        self.assertEqual(report.copywriting_draft, draft)
        self.assertIn("remains a draft", report.warnings[0])

    async def test_director_search_records_provider_sources(self):
        search_result = parse_research({"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "Official guidance.", "annotations": [
                {"type": "url_citation", "url": "https://example.com/guide", "title": "Official guide", "start_index": 0, "end_index": 18}
            ]}]},
        ]}, agent="Creative Director", query="current platform guidance")

        async def search(research, query, *, agent):
            research.results.append(search_result)
            return search_result

        with patch("app.agent.orchestrator.WebResearch.search", new=search):
            report, _, requests, _ = await self.run_script([
                ChatResponse(messages=Message("assistant", [Content.from_function_call("web-1", "search_web", arguments={"query": "current platform guidance"})])),
                self.answer(DirectorPlan(headline="Use current guidance", review=DirectorReview(
                    grounding_checked=True, request_checked=True, reviewed_specialists=[],
                ))),
            ])
        self.assertEqual(report.web_research, [search_result])
        self.assertIn("https://example.com/guide", self.request_text(requests[-1]))

    async def test_web_search_can_be_disabled_per_request(self):
        report, _, requests, _ = await self.run_script([
            self.answer(DirectorPlan(headline="Summary", review=DirectorReview(
                grounding_checked=True, request_checked=True, reviewed_specialists=[],
            ))),
        ], goal="Summarize", allow_web_search=False)
        self.assertFalse(report.web_research)
        self.assertNotIn("search_web", [tool.name for tool in requests[0]["options"].get("tools", [])])
        self.assertIn("Web research is disabled", self.request_text(requests[0]))

    async def test_music_can_search_and_director_receives_sources(self):
        search_result = parse_research({"status": "completed", "output": [
            {"type": "web_search_call", "status": "completed"},
            {"type": "message", "content": [{"type": "output_text", "text": "Official guidance.", "annotations": [
                {"type": "url_citation", "url": "https://example.com/music", "title": "Music guide", "start_index": 0, "end_index": 18}
            ]}]},
        ]}, agent="Music Curator", query="music library guidance")

        async def search(research, query, *, agent):
            self.assertEqual(agent, "Music Curator")
            self.assertEqual(query, "music library guidance")
            research.results.append(search_result)
            return search_result

        with patch("app.agent.orchestrator.WebResearch.search", new=search):
            report, created, requests, _ = await self.run_script([
                self.request("ask_music", "Research music", "Current music guidance needed"),
                ChatResponse(messages=Message("assistant", [Content.from_function_call("web-1", "search_web", arguments={"query": "music library guidance"})])),
                self.answer(MusicPack(cut_to_beat="Preserve the steady rhythm")),
                self.answer(DirectorPlan(headline="Use the library guidance", review=DirectorReview(
                    grounding_checked=True, request_checked=True, reviewed_specialists=["music"],
                ))),
            ], goal="Research suitable music")
        self.assertEqual(created, ["director", "music"])
        self.assertEqual(report.web_research, [search_result])
        self.assertIn("https://example.com/music", self.request_text(requests[-1]))
        self.assertFalse(report.warnings)

    async def test_search_failure_preserves_plan_and_warning(self):
        async def search(research, query, *, agent):
            return research._unavailable(agent, query, "Search provider blocked")

        with patch("app.agent.orchestrator.WebResearch.search", new=search):
            report, _, _, _ = await self.run_script([
                ChatResponse(messages=Message("assistant", [Content.from_function_call("web-1", "search_web", arguments={"query": "trends"})])),
                self.answer(DirectorPlan(headline="Current trends could not be verified", review=DirectorReview(
                    grounding_checked=True, request_checked=True, reviewed_specialists=[],
                ))),
            ])
        self.assertTrue(report.headline)
        self.assertEqual(report.web_research[0].status, "unavailable")
        self.assertIn("Some web research was unavailable", report.warnings[0])


if __name__ == "__main__":
    unittest.main()