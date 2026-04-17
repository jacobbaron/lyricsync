"""Preview MP4 rendering.

v0 is deliberately ugly-but-fast: the original video with the current
lyric line overlaid as plain white text on a semi-transparent black box.
No active-word highlighting, no confidence coloring, no next-line
preview — those are v1.

Implementation: a chain of ``ffmpeg drawtext`` filter entries, one per
aligned line, each gated on a time window via ``enable='between(t,s,e)'``.
We pass the concatenated text through drawtext's ``text=`` parameter
with careful escaping.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from .alignment import AlignmentResult
from .extract import require_ffmpeg


# Characters that need escaping inside a drawtext ``text='...'`` value.
# ffmpeg's filtergraph parser first splits on colons and then drawtext
# interprets ``%{...}`` as an expansion sequence. Single-quoting the
# value handles most punctuation; we still need to escape backslash,
# single quote, colon, and percent.
_DRAWTEXT_ESCAPES = {
    "\\": r"\\",
    "'": r"\'",
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
            f"enable='between(t,{line.start:.3f},{line.end:.3f})'"
        )
    return ",".join(parts)


def render_preview(
    video: Path,
    result: AlignmentResult,
    out_path: Path,
) -> Path:
    """Render a preview MP4 with captions burned in via drawtext.

    Uses a fast encoder preset — this is a verification render, not a
    deliverable. Should complete well under real-time on CPU for a
    typical 3-minute music video.
    """
    ffmpeg = require_ffmpeg()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    vf = build_drawtext_filter(result)
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
