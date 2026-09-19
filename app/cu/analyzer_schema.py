"""TakeTwo's custom Content Understanding schema for video analysis."""

from typing import Any

PACING_VALUES = ["Very Slow", "Slow", "Moderate", "Fast", "Very Fast"]

MOOD_VALUES = [
    "Energetic",
    "Calm",
    "Inspirational",
    "Funny",
    "Dramatic",
    "Informative",
    "Emotional",
    "Aesthetic",
    "Nostalgic",
    "Serious",
]

CATEGORY_VALUES = [
    "Tutorial",
    "Product Demo",
    "Vlog",
    "Talking Head",
    "Behind The Scenes",
    "Before After Transformation",
    "Listicle",
    "Storytime",
    "Comedy Skit",
    "Testimonial",
    "Event Recap",
    "Announcement",
    "Other",
]

AUDIO_STYLE_VALUES = [
    "Voiceover Only",
    "On Camera Dialogue",
    "Music Only",
    "Music And Voice",
    "Ambient Sound",
    "Silent",
]


def reel_field_schema() -> dict[str, Any]:
    return {
        "name": "ReelInsights",
        "description": "Signals needed to critique and improve a video.",
        "fields": {
            "Summary": {
                "type": "string",
                "method": "generate",
                "description": (
                    "Two to four sentences describing what actually happens in the video, "
                    "in chronological order, covering both visuals and speech."
                ),
            },
            "Hook": {
                "type": "string",
                "method": "generate",
                "description": (
                    "Exactly what a viewer sees and hears in the first 3 seconds: the opening "
                    "shot, any on-screen text, and the first spoken words."
                ),
            },
            "HookStrength": {
                "type": "integer",
                "method": "generate",
                "description": (
                    "How well the first 3 seconds stop a scrolling viewer, from 0 (no hook, "
                    "slow logo intro or dead air) to 10 (immediate curiosity, motion, or payoff)."
                ),
            },
            "Pacing": {
                "type": "string",
                "method": "classify",
                "description": "Overall editing tempo based on cut frequency and speech rate.",
                "enum": PACING_VALUES,
            },
            "Mood": {
                "type": "string",
                "method": "classify",
                "description": "Dominant emotional tone of the video.",
                "enum": MOOD_VALUES,
            },
            "ContentCategory": {
                "type": "string",
                "method": "classify",
                "description": "The content format this video follows.",
                "enum": CATEGORY_VALUES,
            },
            "AudioStyle": {
                "type": "string",
                "method": "classify",
                "description": "How audio is used in the video.",
                "enum": AUDIO_STYLE_VALUES,
            },
            "TargetAudience": {
                "type": "string",
                "method": "generate",
                "description": "Who this video appears to be made for, inferred from tone, topic and visuals.",
            },
            "CallToAction": {
                "type": "string",
                "method": "generate",
                "description": (
                    "Any explicit ask made to the viewer (follow, comment, link in bio, subscribe). "
                    "Return an empty string if the video never asks for anything."
                ),
            },
            "SpokenTopics": {
                "type": "array",
                "method": "generate",
                "description": "Key topics or claims made in the spoken audio.",
                "items": {"type": "string"},
            },
            "OnScreenText": {
                "type": "array",
                "method": "generate",
                "description": "Transcribe only legible text overlays, captions and titles visible in the video, in order. Omit unreadable text; do not invent wording.",
                "items": {"type": "string"},
            },
            "BrandsProducts": {
                "type": "array",
                "method": "generate",
                "description": "Brands, products or services shown or mentioned.",
                "items": {"type": "string"},
            },
            "Scenes": {
                "type": "array",
                "method": "generate",
                "description": "Ordered breakdown of the distinct visual beats in the video.",
                "items": {
                    "type": "object",
                    "properties": {
                        "StartTimeMs": {
                            "type": "integer",
                            "method": "generate",
                            "description": "Start time of the beat in milliseconds from the video start.",
                        },
                        "EndTimeMs": {
                            "type": "integer",
                            "method": "generate",
                            "description": "End time of the beat in milliseconds from the video start.",
                        },
                        "Description": {
                            "type": "string",
                            "method": "generate",
                            "description": "What happens visually in this beat.",
                        },
                        "OnScreenText": {
                            "type": "string",
                            "method": "generate",
                            "description": "Transcribe only legible text visible during this beat. Return an empty string if absent or unreadable; do not invent wording.",
                        },
                        "VisualStyle": {
                            "type": "string",
                            "method": "generate",
                            "description": "Shot type, camera movement and lighting, e.g. 'handheld close-up, warm natural light'.",
                        },
                    },
                },
            },
        },
    }


def reel_analyzer_definition(base_analyzer_id: str, locales: list[str] | None = None) -> dict[str, Any]:
    return {
        "description": "TakeTwo - extracts hook, pacing, mood, scenes and transcript from video.",
        "baseAnalyzerId": base_analyzer_id,
        "tags": {"app": "MakeItReel"},
        "models": {"completion": "prebuilt-analyzer-completion"},
        "config": {
            "returnDetails": True,
            "enableSegment": False,
            "locales": locales or ["en-US"],
        },
        "fieldSchema": reel_field_schema(),
    }
