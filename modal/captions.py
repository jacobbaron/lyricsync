"""Styled / word-animated captions rendered with libass (ffmpeg's `ass` filter).

drawtext (timeline._drawtext) draws a whole caption as one block in one font,
so it cannot color, scale or reveal a single word. A text item that names a
`font` or an `anim` style is instead written to an ASS subtitle file built here
and burned in with `ass=`.

Layout is done here, not by libass: every word is its own event pinned with
\\pos to a position computed once per caption from the font's advance widths
(assets/fonts/metrics.json). Line breaks therefore never move when a word is
highlighted, scaled or italicised — libass's own wrapping would re-flow the
line whenever one word's width changed.

Animation styles (`anim`):
    line       the whole caption, static (fade in/out)
    highlight  whole caption shown; the word being sung is tinted (and
               italicised where the font has an italic) with a slight swell
    pop        words appear as they are sung, each with a quick scale/fade-in
    word       one large word at a time, centred

Word timings come from the item's `words`: [{text, start, end}] in seconds
relative to the item's `start` (so they ride along if the item moves). Without
them, `highlight`/`pop`/`word` fall back to `line`.

Stdlib only; imported lazily by timeline.compile_timeline.
"""

from __future__ import annotations

import json
from pathlib import Path

ANIM_STYLES = ("line", "highlight", "pop", "word")

# Font key -> ASS family name (as fontconfig sees the bundled file), the
# metrics.json entry for layout, and the bundled true italic (None: no italic).
FONTS = {
    "instrument-serif": ("Instrument Serif", "InstrumentSerif-Regular.ttf",
                         "InstrumentSerif-Italic.ttf"),
    "playfair": ("Playfair Display SemiBold", "PlayfairDisplay-SemiBold.ttf",
                 "PlayfairDisplay-SemiBoldItalic.ttf"),
    "cormorant": ("Cormorant Garamond SemiBold", "CormorantGaramond-SemiBold.ttf",
                  "CormorantGaramond-SemiBoldItalic.ttf"),
    "dm-serif": ("DM Serif Display", "DMSerifDisplay-Regular.ttf",
                 "DMSerifDisplay-Italic.ttf"),
    "manrope": ("Manrope SemiBold", "Manrope-SemiBold.ttf", None),
    "dm-sans": ("DM Sans 14pt Medium", "DMSans-Medium.ttf", None),
    "montserrat": ("Montserrat SemiBold", "Montserrat-SemiBold.ttf", None),
}
DEFAULT_FONT = "instrument-serif"
DEFAULT_HIGHLIGHT = "#F5C860"  # warm gold
# Serif display faces read small next to a sans at the same point size; these
# multipliers keep `size` meaning roughly the same visual size across fonts.
FONT_SIZE_SCALE = {
    "instrument-serif": 1.45, "cormorant": 1.25, "playfair": 1.1,
    "dm-serif": 1.1, "manrope": 1.0, "dm-sans": 1.0, "montserrat": 1.0,
}

MAX_WIDTH_FRAC = 0.86   # widest a caption row may be, as a fraction of frame width
LINE_HEIGHT = 1.12      # row pitch, in multiples of the font size
WORD_SIZE_SCALE = 1.6   # `word` style: size relative to the item's size
SWELL = 106             # `highlight`: % scale of the sung word (fonts without an italic)
SWELL_MS = 120
WORD_GAP = 0.02         # extra space between words, in multiples of font size

_METRICS: dict | None = None


def _metrics(fonts_dir: str) -> dict:
    global _METRICS
    if _METRICS is None:
        _METRICS = json.loads((Path(fonts_dir) / "metrics.json").read_text())
    return _METRICS


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def text_width(text: str, font_metrics: dict, size: float) -> float:
    adv = font_metrics["adv"]
    avg = font_metrics["avg"]
    units = sum(adv.get(c, avg) for c in text)
    return units * size / font_metrics["em_box"]


def break_rows(widths: list[float], space: float, max_width: float) -> list[int]:
    """Split words into rows no wider than max_width, balanced.

    Returns the row index of each word. Uses the fewest rows greedy wrapping
    allows, then picks the split that minimises the widest row, so a caption
    never ends in an orphaned word when it can be helped.
    """
    n = len(widths)
    if n == 0:
        return []

    def row_w(i: int, j: int) -> float:  # words i..j-1
        return sum(widths[i:j]) + space * (j - i - 1)

    rows = 1
    cur = 0.0
    for k, w in enumerate(widths):
        add = w if k == 0 or cur == 0 else space + w
        if cur and cur + add > max_width:
            rows += 1
            cur = w
        else:
            cur += add

    # best[k][r] = (widest row, breaks) placing the first k words in r rows.
    inf = float("inf")
    best: list[list[tuple[float, list[int]]]] = [
        [(inf, [])] * (rows + 1) for _ in range(n + 1)
    ]
    best[0][0] = (0.0, [])
    for r in range(1, rows + 1):
        for k in range(1, n + 1):
            for i in range(r - 1, k):
                prev, brk = best[i][r - 1]
                if prev == inf:
                    continue
                cand = max(prev, row_w(i, k))
                if cand < best[k][r][0]:
                    best[k][r] = (cand, brk + [i])
    breaks = best[n][rows][1][1:]  # drop the leading 0
    out, row = [], 0
    for k in range(n):
        if row < len(breaks) and k >= breaks[row]:
            row += 1
        out.append(row)
    return out


def layout_words(
    words: list[str], font_key: str, size: float, frame_w: int, frame_h: int,
    position: str, fonts_dir: str, emphasis: bool = False,
) -> list[tuple[float, float]]:
    """Centre point (x, y) of each word, in frame pixels.

    With `emphasis`, each word is given the room it needs in its highlighted
    form too (italic, or swelled for fonts without one), so a sung word never
    crowds its neighbours.
    """
    metrics = _metrics(fonts_dir)
    fm = metrics[FONTS[font_key][1]]
    widths = [text_width(w, fm, size) for w in words]
    if emphasis:
        italic = FONTS[font_key][2]
        if italic:
            widths = [
                max(wd, text_width(w, metrics[italic], size))
                for wd, w in zip(widths, words)
            ]
        else:
            widths = [wd * SWELL / 100 for wd in widths]
    space = text_width(" ", fm, size) + WORD_GAP * size
    rows = break_rows(widths, space, frame_w * MAX_WIDTH_FRAC)
    n_rows = (rows[-1] + 1) if rows else 0
    pitch = size * LINE_HEIGHT
    block_h = pitch * n_rows
    # Same anchors as drawtext captions: top of the block at 10% / 72% of the
    # frame, or the block centred.
    top = {
        "upper": frame_h * 0.10,
        "lower": frame_h * 0.72,
    }.get(position, (frame_h - block_h) / 2)

    centres: list[tuple[float, float]] = []
    for r in range(n_rows):
        idx = [k for k in range(len(words)) if rows[k] == r]
        row_w = sum(widths[k] for k in idx) + space * (len(idx) - 1)
        x = (frame_w - row_w) / 2
        y = top + pitch * r + pitch / 2
        for k in idx:
            centres.append((x + widths[k] / 2, y))
            x += widths[k] + space
    return centres


# ---------------------------------------------------------------------------
# ASS writing
# ---------------------------------------------------------------------------

def _ass_time(t: float) -> str:
    cs = max(0, int(round(t * 100)))
    h, cs = divmod(cs, 360000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


_NAMED_RGB = {
    "white": "FFFFFF", "black": "000000", "gray": "808080",
    "silver": "C0C0C0", "yellow": "FFFF00", "gold": "FFD700",
    "orange": "FFA500", "red": "FF0000", "pink": "FFC0CB",
    "magenta": "FF00FF", "purple": "800080", "blue": "0000FF",
    "cyan": "00FFFF", "green": "008000", "lime": "00FF00",
}


def _ass_color(value: object, default: str) -> str:
    """'#RRGGBB' or a timeline color name -> ASS '&H00BBGGRR&'.

    Values were already whitelisted by timeline.validate_timeline; anything
    unrecognised here falls back to `default`.
    """
    def rgb_of(v: object) -> str | None:
        raw = str(v or "").strip().lower().lstrip("#")
        if raw in _NAMED_RGB:
            return _NAMED_RGB[raw]
        if len(raw) == 6 and all(c in "0123456789abcdef" for c in raw):
            return raw.upper()
        return None

    rgb = rgb_of(value) or rgb_of(default) or "FFFFFF"
    return f"&H00{rgb[4:6]}{rgb[2:4]}{rgb[0:2]}&"


def _sanitize(text: str) -> str:
    # Braces open override blocks and a backslash starts a tag in ASS.
    return text.replace("\\", "").replace("{", "(").replace("}", ")")


def _item_words(item: dict) -> list[dict] | None:
    """Timed words for the item in absolute output seconds, or None."""
    raw = item.get("words")
    if not isinstance(raw, list) or not raw:
        return None
    start, end = float(item["start"]), float(item["end"])
    out = []
    for w in raw:
        try:
            s = start + float(w["start"])
            e = start + float(w.get("end", w["start"]))
        except (KeyError, TypeError, ValueError):
            return None
        text = str(w.get("text") or "").strip()
        if not text or s >= end:
            continue
        out.append({"text": text, "start": max(start, s), "end": min(end, max(e, s))})
    return out or None


def _events_for_item(
    item: dict, frame_w: int, frame_h: int, fonts_dir: str
) -> list[str]:
    font_key = item.get("font") or DEFAULT_FONT
    family, _, italic_file = FONTS[font_key]
    size = float(item.get("size") or 64) * FONT_SIZE_SCALE.get(font_key, 1.0)
    position = str(item.get("position") or "center").lower()
    start, end = float(item["start"]), float(item["end"])
    fade_ms = int(round(float(item.get("fade") or 0) * 1000))
    anim = item.get("anim") or "line"
    timed = _item_words(item)
    if anim != "line" and not timed:
        anim = "line"
    words = [w["text"] for w in timed] if timed else str(item["text"]).split()
    if not words:
        return []

    color = _ass_color(item.get("color"), "white")
    hi = _ass_color(item.get("highlight_color"), DEFAULT_HIGHLIGHT)
    outline = float(item.get("outline") if item.get("outline") is not None else 1.5)
    out_col = _ass_color(item.get("outline_color"), "black")
    # Shared look: thin outline, soft blurred drop shadow (the box drawtext
    # uses reads heavy behind a serif).
    base = (
        f"\\fn{family}\\fs{size:.1f}\\c{color}\\3c{out_col}\\4c&H000000&"
        f"\\4a&H70&\\bord{outline:.2g}\\shad3\\blur1.5\\an5"
    )
    # Fonts with a true italic emphasise the sung word by italicising it; the
    # rest swell it slightly instead. Either way layout_words reserved room.
    italic = "\\i1" if italic_file else ""
    swell = (
        "" if italic_file
        else f"\\t(0,{SWELL_MS},\\fscx{SWELL}\\fscy{SWELL})"
    )
    ev: list[str] = []

    def add(s: float, e: float, x: float, y: float, tags: str, text: str) -> None:
        if e - s < 0.01:
            return
        ev.append(
            f"Dialogue: 0,{_ass_time(s)},{_ass_time(e)},Caption,,0,0,0,,"
            f"{{{base}\\pos({x:.1f},{y:.1f}){tags}}}{_sanitize(text)}"
        )

    def fades(seg_s: float, seg_e: float) -> str:
        fi = fade_ms if abs(seg_s - start) < 1e-6 else 0
        fo = fade_ms if abs(seg_e - end) < 1e-6 else 0
        return f"\\fad({fi},{fo})" if fi or fo else ""

    if anim == "word":
        wsize = size * WORD_SIZE_SCALE
        (x, y), = layout_words(["x"], font_key, wsize, frame_w, frame_h, position, fonts_dir)
        for k, w in enumerate(timed):
            seg_e = timed[k + 1]["start"] if k + 1 < len(timed) else end
            tags = (
                f"\\fs{wsize:.1f}\\fscx80\\fscy80"
                f"\\t(0,90,\\fscx108\\fscy108)\\t(90,180,\\fscx100\\fscy100)"
                + (f"\\fad(0,{fade_ms})" if k + 1 == len(timed) and fade_ms else "")
            )
            add(w["start"], seg_e, x, y, tags, w["text"])
        return ev

    centres = layout_words(
        words, font_key, size, frame_w, frame_h, position, fonts_dir,
        emphasis=anim == "highlight",
    )
    for k, (word, (x, y)) in enumerate(zip(words, centres)):
        if anim == "line":
            add(start, end, x, y, fades(start, end), word)
            continue
        w_s = timed[k]["start"]
        w_e = timed[k + 1]["start"] if k + 1 < len(timed) else end
        if anim == "highlight":
            add(start, w_s, x, y, fades(start, w_s), word)
            add(w_s, w_e, x, y,
                f"\\c{hi}{italic}{swell}"
                + fades(w_s, w_e), word)
            add(w_e, end, x, y, fades(w_e, end), word)
        else:  # pop
            pop = min(400, int((w_e - w_s) * 1000) or 1)
            add(w_s, w_e, x, y,
                f"\\alpha&HFF&\\fscx85\\fscy85\\c{hi}"
                f"\\t(0,140,\\alpha&H00&\\fscx100\\fscy100)\\t(140,{max(141, pop)},\\c{color})"
                + fades(w_s, w_e), word)
            add(w_e, end, x, y, fades(w_e, end), word)
    return ev


def build_ass(items: list[dict], frame_w: int, frame_h: int, fonts_dir: str) -> str:
    """ASS script for the given text items (output-time), sized to the frame."""
    header = (
        "[Script Info]\nScriptType: v4.00+\n"
        f"PlayResX: {frame_w}\nPlayResY: {frame_h}\n"
        "WrapStyle: 2\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        "Style: Caption,Instrument Serif,64,&H00FFFFFF,&H00FFFFFF,&H00000000,"
        "&H70000000,0,0,0,0,100,100,0,0,1,1.5,3,5,0,0,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    events: list[str] = []
    for item in sorted(items, key=lambda i: float(i["start"])):
        events += _events_for_item(item, frame_w, frame_h, fonts_dir)
    return header + "\n".join(events) + "\n"
