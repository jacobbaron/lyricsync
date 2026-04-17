"""SRT output.

Line granularity only in v0 — no word-level karaoke. SRT's timestamp
format is ``HH:MM:SS,mmm`` with a comma decimal separator.
"""

from __future__ import annotations

from pathlib import Path

from .alignment import AlignmentResult


def format_timestamp(seconds: float) -> str:
    """Format seconds as SRT timestamp ``HH:MM:SS,mmm``.

    Negative inputs are clamped to zero. Milliseconds are truncated,
    not rounded — rounding can push an end past the next start.
    """
    if seconds < 0:
        seconds = 0.0
    total_ms = int(seconds * 1000)
    ms = total_ms % 1000
    total_s = total_ms // 1000
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def build_srt(result: AlignmentResult) -> str:
    """Render ``result`` as a complete SRT document string."""
    blocks: list[str] = []
    for i, line in enumerate(result.lines, start=1):
        start = format_timestamp(line.start)
        end = format_timestamp(line.end)
        blocks.append(f"{i}\n{start} --> {end}\n{line.text}\n")
    return "\n".join(blocks)


def write_srt(result: AlignmentResult, out_path: Path) -> Path:
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(build_srt(result), encoding="utf-8")
    return out_path
