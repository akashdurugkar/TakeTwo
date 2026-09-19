"""Maps a raw Content Understanding analyze result onto the VideoSummary model."""

from __future__ import annotations

from typing import Any

from app.models import Scene, TranscriptLine, VideoSummary

_VALUE_KEYS = (
    "valueString",
    "valueInteger",
    "valueNumber",
    "valueBoolean",
    "valueDate",
    "valueTime",
    "valueJson",
)


def field_value(field: Any) -> Any:
    """Unwrap a Content Understanding ContentField into a plain Python value."""
    if not isinstance(field, dict):
        return field
    for key in _VALUE_KEYS:
        if key in field:
            return field[key]
    if "valueArray" in field:
        return [field_value(item) for item in field.get("valueArray") or []]
    if "valueObject" in field:
        return {name: field_value(sub) for name, sub in (field.get("valueObject") or {}).items()}
    return None


def _as_str(fields: dict[str, Any], name: str) -> str:
    value = field_value(fields.get(name))
    return str(value).strip() if value is not None else ""


def _as_int(fields: dict[str, Any], name: str) -> int:
    value = field_value(fields.get(name))
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _as_str_list(fields: dict[str, Any], name: str) -> list[str]:
    value = field_value(fields.get(name))
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if item not in (None, "")]


def _scenes_from_fields(fields: dict[str, Any]) -> list[Scene]:
    value = field_value(fields.get("Scenes"))
    if not isinstance(value, list):
        return []
    scenes: list[Scene] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        scenes.append(
            Scene(
                start_ms=_coerce_int(item.get("StartTimeMs")),
                end_ms=_coerce_int(item.get("EndTimeMs")),
                description=str(item.get("Description") or "").strip(),
                on_screen_text=str(item.get("OnScreenText") or "").strip(),
                visual_style=str(item.get("VisualStyle") or "").strip(),
            )
        )
    return scenes


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _scenes_from_contents(contents: list[dict[str, Any]]) -> list[Scene]:
    """Fallback when a prebuilt analyzer returns one content block per segment."""
    scenes: list[Scene] = []
    for content in contents:
        fields = content.get("fields") or {}
        description = _as_str(fields, "Summary") or _as_str(fields, "Description")
        if not description:
            continue
        scenes.append(
            Scene(
                start_ms=_coerce_int(content.get("startTimeMs")),
                end_ms=_coerce_int(content.get("endTimeMs")),
                description=description,
            )
        )
    return scenes


def to_video_summary(analyze_result: dict[str, Any]) -> VideoSummary:
    result = analyze_result.get("result") or analyze_result
    contents: list[dict[str, Any]] = [c for c in (result.get("contents") or []) if isinstance(c, dict)]
    if not contents:
        return VideoSummary()

    primary = contents[0]
    fields = primary.get("fields") or {}

    transcript: list[TranscriptLine] = []
    key_frames: list[int] = []
    shot_times: list[int] = []
    markdown_parts: list[str] = []
    end_ms = 0

    for content in contents:
        for phrase in content.get("transcriptPhrases") or []:
            transcript.append(
                TranscriptLine(
                    start_ms=_coerce_int(phrase.get("startTimeMs")),
                    end_ms=_coerce_int(phrase.get("endTimeMs")),
                    speaker=str(phrase.get("speaker") or "").strip(),
                    text=str(phrase.get("text") or "").strip(),
                )
            )
        key_frames.extend(_coerce_int(t) for t in content.get("keyFrameTimesMs") or [])
        shot_times.extend(_coerce_int(t) for t in content.get("cameraShotTimesMs") or [])
        if content.get("markdown"):
            markdown_parts.append(str(content["markdown"]))
        end_ms = max(end_ms, _coerce_int(content.get("endTimeMs")))

    summary_text = _as_str(fields, "Summary")
    if len(contents) > 1:
        # Prebuilt analyzers emit a per-segment summary; stitch them into one narrative.
        segment_summaries = [_as_str(c.get("fields") or {}, "Summary") for c in contents]
        summary_text = " ".join(s for s in segment_summaries if s) or summary_text

    scenes = _scenes_from_fields(fields) or _scenes_from_contents(contents)

    return VideoSummary(
        summary=summary_text,
        hook=_as_str(fields, "Hook"),
        hook_strength=_as_int(fields, "HookStrength"),
        pacing=_as_str(fields, "Pacing"),
        mood=_as_str(fields, "Mood"),
        content_category=_as_str(fields, "ContentCategory"),
        target_audience=_as_str(fields, "TargetAudience"),
        call_to_action=_as_str(fields, "CallToAction"),
        audio_style=_as_str(fields, "AudioStyle"),
        spoken_topics=_as_str_list(fields, "SpokenTopics"),
        on_screen_text=_as_str_list(fields, "OnScreenText"),
        brands_products=_as_str_list(fields, "BrandsProducts"),
        scenes=scenes,
        transcript=sorted(transcript, key=lambda line: line.start_ms),
        duration_ms=end_ms,
        width=_coerce_int(primary.get("width")),
        height=_coerce_int(primary.get("height")),
        key_frame_times_ms=sorted(set(key_frames)),
        camera_shot_times_ms=sorted(set(shot_times)),
        markdown="\n\n".join(markdown_parts),
    )
