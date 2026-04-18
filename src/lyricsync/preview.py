"""Preview MP4 rendering.

Burns in captions via an `AnimationRenderer` (default: ASS via libass).
The renderer writes its native caption file (e.g. ``captions.ass``) and
supplies the ``-vf`` filter string; this module just runs ffmpeg.

``build_drawtext_filter`` / ``escape_drawtext`` remain from v0 for unit
tests and as a reference for filtergraph escaping — the drawtext code
path is no longer used at runtime.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .alignment import AlignmentResult
from .animation import AnimationRenderer, StylePreset, get_preset, get_renderer
from .extract import require_ffmpeg


# Characters that need escaping inside a drawtext ``text='...'`` value.
# ffmpeg's filtergraph parser first splits on colons and then drawtext
# interprets ``%{...}`` as an expansion sequence. Single-quoting the
# value handles most punctuation; we still need to escape backslash,
# single quote, colon, and percent.
_DRAWTEXT_ESCAPES = {
    "\\": r"\\",
    "'": r"\'",
    ",": r"\,",
    ":": r"\:",
    "%": r"\%",
}


def escape_drawtext(text: str) -> str:
    """Escape a caption line for use inside a drawtext ``text='...'``."""
    out: list[str] = []
    for ch in text:
        out.append(_DRAWTEXT_ESCAPES.get(ch, ch))
    return "".join(out)


def build_drawtext_filter(result: AlignmentResult) -> str:
    """Build the ``-vf`` filter string for the preview render.

    One drawtext entry per aligned line, each enabled only during its
    [start, end] window. We use a single drawtext per entry chained with
    commas — at any given time at most one is active (assuming the
    aligner produced non-overlapping line windows, which it does for
    v0).
    """
    if not result.lines:
        # No-op filter: copy the input. ``null`` does exactly that.
        return "null"

    parts: list[str] = []
    for line in result.lines:
        text = escape_drawtext(line.text)
        # Bottom-center placement with a semi-transparent box behind the
        # text so it stays legible over any background.
        parts.append(
            "drawtext="
            f"text='{text}':"
            "fontcolor=white:"
            "fontsize=36:"
            "box=1:"
            "boxcolor=black@0.5:"
            "boxborderw=10:"
            "x=(w-text_w)/2:"
            "y=h-(text_h*2):"
            f"enable='between(t\\,{line.start:.3f}\\,{line.end:.3f})'"
        )
    return ",".join(parts)


def render_preview(
    video: Path,
    result: AlignmentResult,
    out_path: Path,
    *,
    renderer: AnimationRenderer | None = None,
    style: StylePreset | None = None,
    caption_path: Path | None = None,
) -> Path:
    """Render a preview MP4 with animated captions burned in.

    Defaults to the ASS backend with the ``classic`` style preset. The
    caption artifact is written next to ``out_path`` (``preview.ass``)
    and kept after render — it's a useful deliverable. Pass
    ``caption_path`` to override its location (e.g. write ``captions.ass``
    into the project directory).
    """
    ffmpeg = require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)

    if renderer is None:
        renderer = get_renderer("ass")
    if style is None:
        style = get_preset("classic")

    # Caption file goes beside the preview unless caller specifies.
    if caption_path is None:
        ext = ".ass" if renderer.name == "ass" else f".{renderer.name}"
        caption_path = out_path.with_suffix(ext)
    renderer.write_caption_file(result, style, caption_path)
    vf = renderer.ffmpeg_video_filter(caption_path)

    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vf",
        vf,
        "-c:v",
        "libx264",
        "-preset",
        "ultrafast",
        "-crf",
        "28",
        "-c:a",
        "copy",
        str(out_path),
    ]
    subprocess.run(cmd, check=True)
    return out_path
