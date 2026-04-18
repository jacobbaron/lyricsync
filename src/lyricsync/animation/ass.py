"""ASS (Advanced SubStation Alpha) animation backend.

Emits an ``.ass`` file that libass (bundled with ffmpeg) renders via the
``ass`` filter. Word-level animation uses ASS karaoke override tags:

  * ``\\k<cs>`` — classic fill, active word switches from SecondaryColour
    to PrimaryColour over ``cs`` centiseconds.
  * ``\\t(t1,t2,...)`` — animated transform between two timestamps.
  * ``\\fad(in,out)`` — line fade in/out in ms.

ASS color format is ``&HAABBGGRR`` (alpha first, then BGR — not RGB).
Alpha is inverted: ``00`` opaque, ``FF`` transparent.

Style-line colors map as follows (ASS quirk worth the comment):

  * ``PrimaryColour``   = color AFTER ``\\k`` fires (sung / active)
  * ``SecondaryColour`` = color BEFORE ``\\k`` fires (upcoming)

So ``style.highlight_color`` -> PrimaryColour and ``style.primary_color``
-> SecondaryColour. Non-intuitive but that's what libass wants.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..alignment import AlignedLine, AlignedWord, AlignmentResult
from .styles import StylePreset


# --------------------------------------------------------------------
# Primitives
# --------------------------------------------------------------------


def _hex_to_ass_color(hex_color: str, alpha: int = 0x00) -> str:
    m = re.fullmatch(r"#?([0-9a-fA-F]{6})", hex_color)
    if not m:
        raise ValueError(
            f"expected color as #RRGGBB or RRGGBB, got {hex_color!r}"
        )
    rrggbb = m.group(1).upper()
    r, g, b = rrggbb[0:2], rrggbb[2:4], rrggbb[4:6]
    return f"&H{alpha:02X}{b}{g}{r}"


def _format_ass_time(sec: float) -> str:
    """Format seconds as ASS timestamp ``H:MM:SS.cs`` (centiseconds)."""
    if sec < 0:
        sec = 0.0
    total_cs = int(sec * 100)  # truncate, don't round, same as SRT
    cs = total_cs % 100
    total_s = total_cs // 100
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h:d}:{m:02d}:{s:02d}.{cs:02d}"


def _alignment_code(position: str) -> int:
    # ASS numpad alignment: 1/2/3 = bottom L/C/R, 4/5/6 = mid, 7/8/9 = top.
    return {"bottom": 2, "center": 5, "top": 8}.get(position, 2)


# --------------------------------------------------------------------
# Section builders
# --------------------------------------------------------------------


def _script_info(resx: int, resy: int) -> str:
    return (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "Collisions: Normal\n"
        f"PlayResX: {resx}\n"
        f"PlayResY: {resy}\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: None\n"
    )


_STYLES_HEADER = (
    "[V4+ Styles]\n"
    "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
    "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
    "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
    "Alignment, MarginL, MarginR, MarginV, Encoding\n"
)


def _style_line(style: StylePreset) -> str:
    # See module docstring re: Primary/Secondary color swap.
    primary = _hex_to_ass_color(style.highlight_color)
    secondary = _hex_to_ass_color(style.primary_color)
    outline = _hex_to_ass_color(style.outline_color)
    back = "&H80000000"  # black @ 50% — used for BorderStyle=3 shadow
    bold = -1 if style.bold else 0
    italic = -1 if style.italic else 0
    alignment = _alignment_code(style.position)
    return (
        "Style: Default,"
        f"{style.font_name},{style.font_size},"
        f"{primary},{secondary},{outline},{back},"
        f"{bold},{italic},0,0,"
        "100,100,0,0,1,"
        f"{style.outline_width},{style.shadow},"
        f"{alignment},60,60,40,1\n"
    )


_EVENTS_HEADER = (
    "[Events]\n"
    "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
    "MarginV, Effect, Text\n"
)


# --------------------------------------------------------------------
# Text / per-word tag generation
# --------------------------------------------------------------------


def _sanitize_text(text: str) -> str:
    """Strip ASS-dangerous characters from caption text.

    ``{`` and ``}`` delimit override tags — any literal brace in lyrics
    would be interpreted as a tag opener. Replace with ordinary quotes.
    Newlines become ASS's ``\\N`` hard break.
    """
    return text.replace("{", "(").replace("}", ")").replace("\n", "\\N")


def _word_tags(
    words: tuple[AlignedWord, ...],
    style: StylePreset,
    line_start: float,
) -> str:
    """Build the Text field body with per-word override tags."""
    if not words:
        return ""
    if style.word_animation == "none":
        return _sanitize_text(" ".join(w.text for w in words))

    parts: list[str] = []
    # Intro delay: silence between line_start and first word's start.
    lead_cs = max(0, int((words[0].start - line_start) * 100))
    if lead_cs > 0:
        parts.append(f"{{\\k{lead_cs}}}")

    for i, w in enumerate(words):
        dur_cs = max(1, int((w.end - w.start) * 100))
        anim = style.word_animation
        if anim == "karaoke":
            tag = f"{{\\k{dur_cs}}}"
        elif anim == "pop":
            # Start at 70% scale, pop to 100% over first 120ms of word.
            tag = (
                f"{{\\k{dur_cs}\\fscx70\\fscy70"
                f"\\t(0,120,\\fscx100\\fscy100)}}"
            )
        elif anim == "fade":
            tag = f"{{\\k{dur_cs}\\fad(80,0)}}"
        else:
            tag = f"{{\\k{dur_cs}}}"
        parts.append(tag + _sanitize_text(w.text))
        if i < len(words) - 1:
            parts.append(" ")
    return "".join(parts)


def _line_prefix(style: StylePreset) -> str:
    fin = style.fade_ms if style.line_in == "fade" else 0
    fout = style.fade_ms if style.line_out == "fade" else 0
    if fin == 0 and fout == 0:
        return ""
    return f"{{\\fad({fin},{fout})}}"


def _dialogue_line(
    line: AlignedLine,
    style: StylePreset,
    *,
    end_override: float | None = None,
) -> str:
    start = _format_ass_time(line.start)
    end = _format_ass_time(
        end_override if end_override is not None else line.end
    )
    prefix = _line_prefix(style)
    if line.words:
        body = _word_tags(line.words, style, line.start)
    else:
        body = _sanitize_text(line.text)
    text = prefix + body
    return f"Dialogue: 0,{start},{end},Default,,0,0,0,,{text}\n"


# Minimum visual gap between adjacent Dialogue events so line N's fade-out
# never overlaps line N+1's fade-in. 10ms is below human perception and
# still safely > 1 centisecond (ASS's timestamp granularity).
_MIN_INTER_LINE_GAP_SEC = 0.01


def _clamped_end(line: AlignedLine, next_line: AlignedLine | None) -> float:
    """Return the effective end time for a Dialogue, clamped so it never
    extends past the next line's start (minus a small gap)."""
    if next_line is None:
        return line.end
    max_end = next_line.start - _MIN_INTER_LINE_GAP_SEC
    # If the aligner produced an already-overlapping pair, prefer the
    # earlier start over the later end — the next line wins.
    return min(line.end, max_end) if max_end > line.start else line.start


# --------------------------------------------------------------------
# Public entry points
# --------------------------------------------------------------------


def build_ass(
    result: AlignmentResult,
    style: StylePreset,
    video_size: tuple[int, int] | None = None,
) -> str:
    """Render an AlignmentResult + style preset as a complete .ass doc.

    ``video_size`` sets PlayResX/PlayResY; libass scales layout to the
    actual render resolution but text sizing math works best when this
    roughly matches the target video. Defaults to 1920x1080.
    """
    resx, resy = video_size or (1920, 1080)
    out: list[str] = [
        _script_info(resx, resy),
        "\n",
        _STYLES_HEADER,
        _style_line(style),
        "\n",
        _EVENTS_HEADER,
    ]
    lines = [
        L for L in result.lines if L.words or L.text.strip()
    ]
    for i, line in enumerate(lines):
        next_line = lines[i + 1] if i + 1 < len(lines) else None
        end = _clamped_end(line, next_line)
        out.append(_dialogue_line(line, style, end_override=end))
    return "".join(out)


@dataclass(frozen=True)
class AssRenderer:
    name: str = "ass"

    def write_caption_file(
        self,
        result: AlignmentResult,
        style: StylePreset,
        out_path: Path,
        video_size: tuple[int, int] | None = None,
    ) -> Path:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            build_ass(result, style, video_size=video_size),
            encoding="utf-8",
        )
        return out_path

    def ffmpeg_video_filter(self, caption_path: Path) -> str:
        # Same escaping as the legacy SRT path: the ffmpeg filtergraph
        # parser splits on ``:`` and treats ``'`` / ``\`` specially.
        abs_path = str(caption_path.resolve())
        escaped = (
            abs_path.replace("\\", r"\\")
            .replace(":", r"\:")
            .replace("'", r"\'")
        )
        return f"ass='{escaped}'"
