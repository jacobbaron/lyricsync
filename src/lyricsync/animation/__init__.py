"""Caption animation backends.

Abstracts "turn an AlignmentResult + StylePreset into a burn-in caption
artifact" so the preview pipeline can target different renderers (ASS
via libass, browser-render via headless Chromium, canvas via Pillow,
etc.) without rewiring the CLI or preview glue.

A backend implements `AnimationRenderer`:
  - `write_caption_file(result, style, out_path)` writes the engine's
    native caption file (e.g. ``.ass``).
  - `ffmpeg_video_filter(caption_path)` returns the ``-vf`` string that
    ffmpeg uses to burn the captions in during preview render.

Backends register themselves via `register_renderer`; callers look them
up by name with `get_renderer`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol, runtime_checkable

from ..alignment import AlignmentResult
from .styles import BUILTIN_PRESETS, StylePreset, get_preset

__all__ = [
    "AnimationRenderer",
    "StylePreset",
    "BUILTIN_PRESETS",
    "get_preset",
    "get_renderer",
    "register_renderer",
    "available_renderers",
]


@runtime_checkable
class AnimationRenderer(Protocol):
    """Turn an alignment + style into a burn-in caption artifact.

    Implementations translate the shared style vocabulary into whatever
    their engine speaks. Adding a new backend is: implement this
    Protocol, call `register_renderer` from your module.
    """

    name: str

    def write_caption_file(
        self,
        result: AlignmentResult,
        style: StylePreset,
        out_path: Path,
        video_size: tuple[int, int] | None = None,
    ) -> Path: ...

    def ffmpeg_video_filter(self, caption_path: Path) -> str: ...


_registry: dict[str, AnimationRenderer] = {}


def register_renderer(renderer: AnimationRenderer) -> None:
    _registry[renderer.name] = renderer


def get_renderer(name: str) -> AnimationRenderer:
    if name not in _registry:
        raise KeyError(
            f"unknown animation backend: {name!r} "
            f"(have {sorted(_registry)})"
        )
    return _registry[name]


def available_renderers() -> list[str]:
    return sorted(_registry)


# Register built-ins. Import here (not at top) to avoid a circular
# import: ass.py imports StylePreset from .styles.
from .ass import AssRenderer  # noqa: E402

register_renderer(AssRenderer())
