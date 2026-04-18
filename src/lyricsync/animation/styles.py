"""Style presets for caption animation.

A `StylePreset` is backend-agnostic: it names the knobs a typical caption
animation system exposes (font, colors, outlines, word-level animation,
in/out transitions). Each backend translates these into its own
override syntax — the ASS backend emits karaoke tags, a future browser
backend would emit CSS keyframes.

`backend_params` is an escape hatch for features that don't map across
engines (raw ASS override tags, CSS selectors, etc.). Generic code
should ignore it; backends can peek at their own keys.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

Position = Literal["bottom", "center", "top"]
WordAnim = Literal["karaoke", "pop", "fade", "none"]
LineTrans = Literal["fade", "none"]


@dataclass(frozen=True)
class StylePreset:
    """Backend-agnostic caption style + animation config."""

    name: str
    description: str = ""

    # Typography
    font_name: str = "Arial"
    font_size: int = 48
    bold: bool = False
    italic: bool = False

    # Colors — hex #RRGGBB. `primary_color` is the resting color;
    # `highlight_color` is what active words become (karaoke fill).
    primary_color: str = "#FFFFFF"
    highlight_color: str = "#F5A623"
    outline_color: str = "#000000"

    # Decoration
    outline_width: float = 2.0
    shadow: float = 1.0

    # Layout
    position: Position = "bottom"

    # Animation
    word_animation: WordAnim = "karaoke"
    line_in: LineTrans = "fade"
    line_out: LineTrans = "fade"
    fade_ms: int = 150

    # Backend-specific overrides (e.g. raw ASS tags). Keep empty for
    # portable presets.
    backend_params: dict[str, Any] = field(default_factory=dict)


BUILTIN_PRESETS: dict[str, StylePreset] = {
    "classic": StylePreset(
        name="classic",
        description=(
            "White lyrics with an amber karaoke fill, black outline, "
            "bottom-center position. Safe default."
        ),
    ),
    "pop": StylePreset(
        name="pop",
        description=(
            "Bold display font. Each word pops in (scale-up) at its "
            "start time. Bright, punchy — TikTok-style."
        ),
        font_name="Impact",
        font_size=72,
        bold=True,
        primary_color="#FFFFFF",
        highlight_color="#FFE14C",
        outline_color="#111122",
        outline_width=3.5,
        shadow=2.0,
        word_animation="pop",
        fade_ms=120,
    ),
    "neon": StylePreset(
        name="neon",
        description=(
            "Cyan glow karaoke fill on a dark outline. Softer pace."
        ),
        font_name="Arial",
        font_size=56,
        bold=True,
        primary_color="#B8F1FF",
        highlight_color="#22D3EE",
        outline_color="#0B1020",
        outline_width=2.5,
        shadow=3.0,
        word_animation="karaoke",
        fade_ms=200,
    ),
    "plain": StylePreset(
        name="plain",
        description=(
            "Line-level only, no word animation — matches the old SRT "
            "preview."
        ),
        word_animation="none",
        line_in="none",
        line_out="none",
    ),
}


def get_preset(name: str) -> StylePreset:
    if name not in BUILTIN_PRESETS:
        raise KeyError(
            f"unknown style preset: {name!r} "
            f"(have {sorted(BUILTIN_PRESETS)})"
        )
    return BUILTIN_PRESETS[name]


def available_presets() -> list[str]:
    return sorted(BUILTIN_PRESETS)
