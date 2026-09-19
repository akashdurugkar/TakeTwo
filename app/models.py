"""Domain models: what Content Understanding extracts, and what TakeTwo produces."""

from typing import Annotated, Literal

from pydantic import BaseModel, Field, StringConstraints

Platform = Literal["general", "instagram_reels", "tiktok", "youtube_shorts", "youtube", "linkedin", "website"]
VideoPurpose = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=200)]


# --------------------------------------------------------------------------
# Stage 1 -- Content Understanding: "What IS in the video?"
# --------------------------------------------------------------------------


class Scene(BaseModel):
    start_ms: int = 0
    end_ms: int = 0
    description: str = ""
    on_screen_text: str = ""
    visual_style: str = ""


class TranscriptLine(BaseModel):
    start_ms: int = 0
    end_ms: int = 0
    speaker: str = ""
    text: str = ""


class VideoSummary(BaseModel):
    """Structured description of the video as it exists today."""

    summary: str = ""
    hook: str = Field("", description="What happens in the first ~3 seconds")
    hook_strength: int = Field(0, ge=0, le=10)
    pacing: str = ""
    mood: str = ""
    content_category: str = ""
    target_audience: str = ""
    call_to_action: str = ""
    audio_style: str = ""
    spoken_topics: list[str] = Field(default_factory=list)
    on_screen_text: list[str] = Field(default_factory=list)
    brands_products: list[str] = Field(default_factory=list)
    scenes: list[Scene] = Field(default_factory=list)
    transcript: list[TranscriptLine] = Field(default_factory=list)

    duration_ms: int = 0
    width: int = 0
    height: int = 0
    key_frame_times_ms: list[int] = Field(default_factory=list)
    camera_shot_times_ms: list[int] = Field(default_factory=list)
    markdown: str = ""

    @property
    def aspect_ratio(self) -> str:
        if not self.width or not self.height:
            return "unknown"
        ratio = self.width / self.height
        if abs(ratio - 9 / 16) < 0.05:
            return "9:16"
        if abs(ratio - 1) < 0.05:
            return "1:1"
        if abs(ratio - 16 / 9) < 0.05:
            return "16:9"
        return f"{self.width}:{self.height}"

    def to_agent_brief(self) -> str:
        """Compact, token-friendly rendering handed to the TakeTwo creative team."""
        lines = [
            f"Duration: {self.duration_ms / 1000:.1f}s",
            f"Resolution: {self.width}x{self.height} (aspect {self.aspect_ratio})",
            f"Shot changes: {len(self.camera_shot_times_ms)}",
            f"Summary: {self.summary}",
            f"Current hook (first 3s): {self.hook}",
            f"Hook strength (self-assessed 0-10): {self.hook_strength}",
            f"Pacing: {self.pacing}",
            f"Mood: {self.mood}",
            f"Category: {self.content_category}",
            f"Target audience: {self.target_audience}",
            f"Existing CTA: {self.call_to_action or 'none'}",
            f"Audio style: {self.audio_style}",
        ]
        if self.spoken_topics:
            lines.append(f"Spoken topics: {', '.join(self.spoken_topics)}")
        if self.on_screen_text:
            lines.append(f"On-screen text: {' | '.join(self.on_screen_text[:15])}")
        if self.brands_products:
            lines.append(f"Brands/products: {', '.join(self.brands_products)}")
        if self.scenes:
            lines.append("Scenes:")
            for scene in self.scenes[:20]:
                lines.append(
                    f"  [{scene.start_ms / 1000:.1f}s-{scene.end_ms / 1000:.1f}s] "
                    f"{scene.description} (text: {scene.on_screen_text or '-'}, "
                    f"style: {scene.visual_style or '-'})"
                )
        if self.transcript:
            lines.append("Transcript:")
            for line in self.transcript[:60]:
                lines.append(f"  [{line.start_ms / 1000:.1f}s] {line.speaker}: {line.text}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# Stage 2 -- TakeTwo creative team: "How can we IMPROVE it?"
# --------------------------------------------------------------------------


class HookSuggestion(BaseModel):
    text: str = Field(description="The spoken or on-screen line for the first 3 seconds")
    rationale: str = Field(description="Why this hook works for this specific video")
    delivery: str = Field(description="How to shoot/edit it, e.g. 'hard cut to close-up, text overlay'")


class CaptionSuggestion(BaseModel):
    text: str = Field(description="Full caption copy, ready to paste")
    tone: str = ""
    character_count: int = 0


class MusicRecommendation(BaseModel):
    track_style: str = Field(description="Style/genre of track to use")
    bpm_range: str = ""
    reason: str = ""
    search_terms: list[str] = Field(default_factory=list, description="Terms to search in the platform audio library")


class EditSuggestion(BaseModel):
    timestamp: str = Field(description="Where in the video to apply this, e.g. '0:00-0:03'")
    change: str = Field(description="The concrete edit to make")
    impact: str = Field(description="Expected effect on retention or engagement")
    priority: Literal["high", "medium", "low"] = "medium"


class OverlayText(BaseModel):
    timestamp: str = Field(description="When the overlay appears, e.g. '0:02-0:05'")
    text: str = Field(description="The exact words to burn into the frame")
    placement: str = Field(description="Where it sits, respecting the platform's safe margins")


# -- Creative Director, pass 1: the brief the specialists work from ---------


class CreativeBrief(BaseModel):
    angle: str = Field(description="The single creative angle every specialist must serve")
    target_emotion: str = Field(description="What the viewer should feel by the end")
    core_promise: str = Field(description="The one promise the first 3 seconds makes to the viewer")
    priorities: list[str] = Field(
        default_factory=list, description="Ranked fixes, highest impact first"
    )
    do_not: list[str] = Field(
        default_factory=list, description="Things the specialists must avoid for this specific video"
    )


# -- Specialist outputs ----------------------------------------------------


class Critique(BaseModel):
    """Critic agent: scores the video, plans the edit, and picks the cover frame."""

    overall_score: int = Field(ge=0, le=100, description="Editorial assessment of the original video out of 100, not measured performance")
    verdict: str = Field(description="Two sentences on the video's biggest opportunity")
    strengths: list[str] = Field(default_factory=list)
    risks: list[str] = Field(default_factory=list)
    edit_suggestions: list[EditSuggestion] = Field(default_factory=list)
    cover_frame_ms: int = Field(0, description="Timestamp of the frame to use as the cover, in milliseconds")
    cover_reason: str = ""
    cover_text: str = Field("", description="Three to five words to overlay on the cover frame")


class CopyPack(BaseModel):
    """Copywriter agent: everything the viewer reads or hears, plus how it is tagged."""

    better_hooks: list[HookSuggestion] = Field(default_factory=list)
    captions: list[CaptionSuggestion] = Field(default_factory=list)
    cta_suggestions: list[str] = Field(default_factory=list)
    overlay_text: list[OverlayText] = Field(default_factory=list)
    hashtags: list[str] = Field(default_factory=list)


class MusicPack(BaseModel):
    """Music directions and optional sourced candidates, not a license grant."""

    music: list[MusicRecommendation] = Field(default_factory=list)
    cut_to_beat: str = Field("", description="How to align cuts to the track's rhythm")


# -- Creative Director, pass 2: the synthesised plan ------------------------


class PlanBeat(BaseModel):
    start: str = Field(description="Start in the proposed edit as m:ss or m:ss.s, not a timestamp in the original video")
    end: str = Field(description="End in the proposed edit as m:ss or m:ss.s, not a timestamp in the original video")
    source_start_ms: int | None = Field(None, ge=0, description="Approximate source clip start in ORIGINAL video milliseconds, supported by supplied evidence; null if unknown or new footage")
    source_end_ms: int | None = Field(None, ge=0, description="Approximate source clip end in ORIGINAL video milliseconds; null if unknown or new footage")
    action: Literal["keep", "trim", "move", "reshoot", "new_shot", "unspecified"] = Field("unspecified", description="Primary edit action; move only when changing shot order, not merely shifting timestamps after a trim")
    timing: Literal["normal", "slow_motion", "speed_up", "freeze_frame", "loop", "unspecified"] = Field("unspecified", description="How source footage fills the proposed duration; never imply normal speed when durations differ")
    timing_note: str = Field("", description="Concrete duration treatment when needed, e.g. 0.5s played at 1/6 speed for 3s; disclose quality/frame-rate uncertainty")
    say: str = Field(description="What is spoken over this beat, or '-' if silent")
    show: str = Field(description="Shot description or new footage to capture; source timestamps belong in source_start_ms/source_end_ms, not this text")
    overlay: str = Field(description="Burned-in text for this beat, or '-'")
    why: str = Field(description="What this beat is doing for retention")


class SkippedSpecialist(BaseModel):
    specialist: Literal["critic", "copywriter", "music"]
    reason: str = Field(min_length=1, description="Brief user-visible reason this specialist was not requested")


class DirectorReview(BaseModel):
    grounding_checked: bool = Field(description="Final recommendations and copy were checked against the supplied evidence")
    request_checked: bool = Field(description="Final deliverables were checked against the user's goal and restrictions")
    reviewed_specialists: list[Literal["critic", "copywriter", "music"]]
    notes: list[str] = Field(default_factory=list, description="Brief review outcomes or corrections, not hidden reasoning")
    claims_to_confirm: list[str] = Field(default_factory=list, description="Unverified facts/questions kept out of final copy")


class DirectorPlan(BaseModel):
    """The Director's final response after any requested specialist work."""

    headline: str = Field(description="One line the creator should act on first")
    response: str = Field("", description="Direct conversational answer to the current message, with explanations or requested changes")
    brief: CreativeBrief | None = None
    timeline: list[PlanBeat] = Field(default_factory=list)
    ranked_fixes: list[str] = Field(default_factory=list, description="Ordered to-do list")
    chosen_hook: str = Field("", description="The hook option the Director is backing, verbatim")
    chosen_hook_reason: str = ""
    copywriting: CopyPack | None = Field(None, description="Director-reviewed and corrected copy; null when no Copywriter report is available")
    review: DirectorReview | None = None
    skipped_specialists: list[SkippedSpecialist] = Field(default_factory=list)
    conflicts_resolved: list[str] = Field(
        default_factory=list, description="Disagreements between specialists and how they were settled"
    )


# -- Agent tracing ---------------------------------------------------------

AgentStatus = Literal["pending", "running", "succeeded", "failed", "skipped"]


class AgentRun(BaseModel):
    name: str
    role: str
    status: AgentStatus = "pending"
    duration_ms: int = 0
    error: str = ""
    task: str = ""
    reason: str = ""


# -- The final deliverable -------------------------------------------------


class WebCitation(BaseModel):
    title: str
    url: str
    start_index: int
    end_index: int


class WebResearchResult(BaseModel):
    agent: str
    query: str
    searched_at: str
    status: Literal["succeeded", "failed", "unavailable"]
    answer: str = ""
    citations: list[WebCitation] = Field(default_factory=list)
    error: str = ""


class ReelPlan(BaseModel):
    """TakeTwo's final video improvement plan; the type name is retained for compatibility."""

    overall_score: int | None = Field(None, ge=0, le=100)
    verdict: str = ""
    headline: str = ""
    response: str = ""
    brief: CreativeBrief | None = None
    timeline: list[PlanBeat] = Field(default_factory=list)
    ranked_fixes: list[str] = Field(default_factory=list)
    chosen_hook: str = ""
    chosen_hook_reason: str = ""
    conflicts_resolved: list[str] = Field(default_factory=list)

    critique: Critique | None = None
    copywriting: CopyPack | None = None
    copywriting_draft: CopyPack | None = None
    music: MusicPack | None = None
    review: DirectorReview | None = None
    web_research: list[WebResearchResult] = Field(default_factory=list)

    agent_runs: list[AgentRun] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


# --------------------------------------------------------------------------
# Job envelope
# --------------------------------------------------------------------------

class DirectorChatTurn(BaseModel):
    id: str
    message: str
    goal: str
    use_web_search: bool = False
    status: Literal["running", "succeeded", "failed"] = "running"
    created_at: str
    answer: str = ""
    report: ReelPlan | None = None
    error: str = ""
    stage_message: str = "Creative Director is reading the saved analysis..."
    agent_runs: list[AgentRun] = Field(default_factory=list)


class DirectorConversation(BaseModel):
    goal: str = ""
    turns: list[DirectorChatTurn] = Field(default_factory=list)


JobStatus = Literal["queued", "analyzing", "briefing", "advising", "synthesizing", "succeeded", "failed"]


class Job(BaseModel):
    id: str
    blob_upload_id: str = ""
    blob_upload_active: bool = False
    blob_cleanup_pending: bool = False
    status: JobStatus = "queued"
    filename: str = ""
    source_url: str = ""
    target_platform: Platform = "instagram_reels"
    video_purpose: VideoPurpose = "General review"
    goal: str = ""
    use_web_search: bool = True
    stage_message: str = ""
    error: str = ""
    analyzer_id: str = ""
    video_summary: VideoSummary | None = None
    agent_runs: list[AgentRun] = Field(default_factory=list)
    report: ReelPlan | None = None
    director_chat: DirectorConversation = Field(default_factory=DirectorConversation)
