"""Tools that measure the video. They report facts only — every judgement belongs to an agent."""

from __future__ import annotations

import json
import re
from typing import Annotated

from pydantic import Field


def measure_pacing(
    duration_ms: Annotated[int, Field(description="Total video duration in milliseconds.")],
    camera_shot_times_ms: Annotated[
        str, Field(description="Comma-separated camera shot-change timestamps in milliseconds, from the analysis.")
    ],
) -> str:
    """Measure the video's edit rhythm: cut rate, gaps between cuts, and where the longest static stretch is."""
    cuts = _parse_ms(camera_shot_times_ms)
    duration_s = duration_ms / 1000 if duration_ms > 0 else 0.0
    if duration_s <= 0:
        return json.dumps({"error": "duration_ms must be greater than zero."})

    gaps = [round((b - a) / 1000, 2) for a, b in zip(cuts, cuts[1:])] if len(cuts) > 1 else []
    longest_gap = max(gaps) if gaps else round(duration_s, 2)
    longest_gap_start = (
        cuts[gaps.index(longest_gap)] if gaps else 0
    )

    return json.dumps(
        {
            "duration_seconds": round(duration_s, 2),
            "shot_count": len(cuts),
            "cuts_per_10_seconds": round(len(cuts) / duration_s * 10, 2),
            "first_cut_at_ms": cuts[0] if cuts else None,
            "gaps_between_cuts_seconds": gaps,
            "longest_static_stretch_seconds": longest_gap,
            "longest_static_stretch_starts_at_ms": longest_gap_start,
        }
    )


def list_cover_frame_candidates(
    key_frame_times_ms: Annotated[
        str, Field(description="Comma-separated key frame timestamps in milliseconds, from the analysis.")
    ],
    duration_ms: Annotated[int, Field(description="Total video duration in milliseconds.")],
) -> str:
    """List every available cover-frame timestamp with how far into the video it sits."""
    frames = _parse_ms(key_frame_times_ms)
    if not frames or duration_ms <= 0:
        return json.dumps({"candidates": []})

    return json.dumps(
        {
            "candidates": [
                {"time_ms": ms, "position_percent": round(ms / duration_ms * 100, 1)} for ms in frames
            ]
        }
    )


def _parse_ms(raw: str) -> list[int]:
    values: list[int] = []
    for token in re.split(r"[,\s]+", raw or ""):
        token = token.strip()
        if not token:
            continue
        try:
            values.append(int(float(token)))
        except ValueError:
            continue
    return sorted(set(values))


DIRECTOR_TOOLS = [measure_pacing]
CRITIC_TOOLS = [measure_pacing, list_cover_frame_candidates]
COPYWRITER_TOOLS: list = []
MUSIC_TOOLS = [measure_pacing]
