"""Lyric forced alignment — pure helpers.

No Modal/torch/network deps — stdlib only — so it imports cleanly in tests.
modal/app.py imports these helpers; Modal automounts this local module on
deploy (see the align_image mount list).

The problem this solves: ASR transcription of *sung* audio is unreliable
(mondegreens, dropped lines, whole sections missed under loud instrumentation),
but the lyrics are already known. Forced alignment inverts the problem — the
text is fixed and only the timing is solved for.

Feeding the whole lyric sheet to wav2vec2 as one segment spanning the whole
track does not work well in practice: with long instrumental passages and held
notes the aligner has too much freedom, and lines adjacent to a window edge get
dragged toward it by several seconds (interior lines stay stable — it is an
edge effect). So we window the audio first, using the existing raw ASR as
*anchors*: wherever the ASR independently heard the same word the lyric sheet
claims, that timestamp is evidence, even when the surrounding words were
misheard. Each window then carries only a line or two of known text over a few
seconds of audio, which wav2vec2 pins down tightly.

Pipeline:
    parse_lyrics_text  → lines (section headings and blanks dropped)
    plan_windows       → [{lines, start, end}] via ASR anchors
    (caller runs wav2vec2 per window)
    aggregate_words    → per-line {text, start, end, score}
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass

# A bracketed line like "[Chorus]" or "[Verse 2]" is structure, not lyric.
_SECTION_RE = re.compile(r"^\s*[\[(].{0,40}[\])]\s*$")
_TOKEN_RE = re.compile(r"[a-z0-9']+")

# Number words the ASR routinely renders as digits ("Five more" → "5 more").
# Normalizing both directions lets those tokens serve as anchors.
_NUMBERS = {
    "0": "zero", "1": "one", "2": "two", "3": "three", "4": "four",
    "5": "five", "6": "six", "7": "seven", "8": "eight", "9": "nine",
    "10": "ten",
}

# A matching run must be at least this many tokens to be trusted as an anchor.
# Single-token matches are too easy to hit by chance in a lyric full of
# repeated words ("five", "more", "away") and a spurious anchor drags a whole
# window to the wrong place.
MIN_ANCHOR_RUN = 2

# --- ASR hygiene -----------------------------------------------------------
# Whisper degrades in two characteristic ways on sung audio, and both produce
# anchors that are confidently wrong:
#   1. Repetition loops — it emits the same phrase many times over, every word
#      stamped at essentially the same instant.
#   2. Held vowels — a sustained note gets one word with a 20-second duration.
# Neither is usable as evidence, so they are dropped/clamped before matching.
HALLUCINATION_RUN = 5          # words...
HALLUCINATION_SPAN_S = 0.5     # ...packed into this span = a repetition loop
MAX_ANCHOR_WORD_S = 2.0        # clamp a held word's end for anchoring purposes

# Floor on how much audio a window must offer the text it carries. A window
# tighter than this starves the aligner (it emits a sub-second line with a
# near-zero score), so such a window is merged into a neighbour instead.
MIN_S_PER_TOKEN = 0.22
MIN_WINDOW_S = 1.2

# Padding around an anchored window. The tail is looser than the head because
# sung vowels are held well past the point the aligner stops scoring them, and
# because a window that ends too early truncates the final word outright.
PAD_HEAD_S = 0.6
PAD_TAIL_S = 1.0


@dataclass(frozen=True)
class Window:
    """One alignment unit: known text + the audio span to solve it against."""

    lines: tuple[str, ...]
    start: float
    end: float
    anchored: bool  # False = span inferred from neighbours, timings are softer

    def as_segment(self) -> dict:
        """The dict shape whisperx.align() wants."""
        return {"text": " ".join(self.lines), "start": self.start, "end": self.end}


def normalize_token(text: str) -> str:
    """Fold a word to its comparison form (lowercase, no punctuation, digits
    spelled out). Returns '' for tokens that carry no letters or digits."""
    m = _TOKEN_RE.findall(str(text).lower())
    if not m:
        return ""
    tok = max(m, key=len)
    return _NUMBERS.get(tok, tok)


def parse_lyrics_text(raw: str) -> list[str]:
    """Split a lyric sheet into caption lines.

    Blank lines and bracketed section headings ("[Chorus]") are dropped — they
    are structure for the songwriter, not text anyone sings. Everything else is
    one caption line, kept verbatim (punctuation and capitalization intact);
    only the *matching* is normalized.
    """
    lines: list[str] = []
    for raw_line in str(raw).splitlines():
        line = raw_line.strip()
        if not line or _SECTION_RE.match(line):
            continue
        if not any(normalize_token(w) for w in line.split()):
            continue
        lines.append(line)
    return lines


def _flatten(lines: list[str]) -> list[tuple[int, str]]:
    """(line index, normalized token) for every token, in document order."""
    out: list[tuple[int, str]] = []
    for i, line in enumerate(lines):
        for word in line.split():
            tok = normalize_token(word)
            if tok:
                out.append((i, tok))
    return out


def clean_asr_words(asr_words: list[dict]) -> list[tuple[str, float, float]]:
    """Normalize the raw ASR word list into usable (token, start, end) anchors.

    Drops untimed and untokenizable words, drops Whisper repetition loops (a
    run of words sharing one instant), clamps held vowels, and enforces
    monotonic time — a non-monotonic word is the tail of a hallucination.
    """
    parsed: list[tuple[str, float, float]] = []
    for w in asr_words:
        tok = normalize_token(w.get("text", ""))
        start = w.get("local_start", w.get("start"))
        if not tok or start is None:
            continue
        end = w.get("local_end", w.get("end"))
        start = float(start)
        end = float(end) if end is not None else start
        parsed.append((tok, start, max(start, end)))

    # Flag repetition loops: any word inside a dense run of HALLUCINATION_RUN
    # words spanning less than HALLUCINATION_SPAN_S.
    bad = set()
    for i in range(len(parsed)):
        j = i + HALLUCINATION_RUN - 1
        if j < len(parsed) and parsed[j][2] - parsed[i][1] < HALLUCINATION_SPAN_S:
            bad.update(range(i, j + 1))

    out: list[tuple[str, float, float]] = []
    last = float("-inf")
    for i, (tok, start, end) in enumerate(parsed):
        if i in bad or start < last:
            continue
        out.append((tok, start, min(end, start + MAX_ANCHOR_WORD_S)))
        last = start
    return out


def line_anchors(
    lines: list[str], asr_words: list[dict]
) -> dict[int, tuple[float, float]]:
    """Map line index → (earliest, latest) ASR time confidently on that line.

    Aligns the lyric token stream against the ASR token stream with difflib and
    keeps only matching runs of at least MIN_ANCHOR_RUN tokens. A line the ASR
    misheard or skipped entirely simply gets no entry.

    asr_words: [{text, local_start, local_end}, ...] in time order — the raw
    per-word transcript already stored for the clip.
    """
    lyric_toks = _flatten(lines)
    asr = clean_asr_words(asr_words)

    if not lyric_toks or not asr:
        return {}

    matcher = difflib.SequenceMatcher(
        a=[t for _, t in lyric_toks], b=[t for t, _, _ in asr], autojunk=False
    )
    anchors: dict[int, tuple[float, float]] = {}
    for i, j, size in matcher.get_matching_blocks():
        if size < MIN_ANCHOR_RUN:
            continue
        for k in range(size):
            line_idx = lyric_toks[i + k][0]
            _, start, end = asr[j + k]
            lo, hi = anchors.get(line_idx, (start, end))
            anchors[line_idx] = (min(lo, start), max(hi, end))
    return anchors


def plan_windows(
    lines: list[str],
    asr_words: list[dict],
    duration: float,
    pad_head: float = PAD_HEAD_S,
    pad_tail: float = PAD_TAIL_S,
) -> list[Window]:
    """Group lines into audio windows for forced alignment.

    An anchored line becomes its own window around its ASR evidence. Runs of
    unanchored lines (a section the ASR missed wholesale) are grouped into one
    window spanning the gap between the surrounding anchors, so the aligner
    distributes them across exactly the audio that is left over.

    Windows are clamped to [0, duration] and kept non-overlapping in order, so
    a held note at the end of one line cannot swallow the start of the next.
    """
    if not lines:
        return []
    anchors = line_anchors(lines, asr_words)

    # A run of identical lines (a chorus repeated N times) cannot be anchored
    # line-by-line: every repeat matches the ASR equally well, so difflib pairs
    # them arbitrarily and the repeats land scattered across the section. Drop
    # anchors inside such a run and let the whole run share one window — the
    # aligner then distributes the repeats evenly across exactly the audio
    # bounded by the distinct lines on either side, which is what a repeated
    # refrain actually sounds like.
    for idx in _repeated_line_indices(lines):
        anchors.pop(idx, None)

    # Group consecutive lines by whether they carry anchors.
    groups: list[tuple[bool, list[int]]] = []
    for idx in range(len(lines)):
        has = idx in anchors
        if groups and groups[-1][0] == has:
            groups[-1][1].append(idx)
        else:
            groups.append((has, [idx]))

    # Anchored groups are split one-window-per-line; unanchored groups stay
    # whole. Resolve each group's span first, then fill unanchored spans from
    # their neighbours.
    spans: list[tuple[list[int], float | None, float | None, bool]] = []
    for has, idxs in groups:
        if has:
            for idx in idxs:
                lo, hi = anchors[idx]
                spans.append(([idx], lo - pad_head, hi + pad_tail, True))
        else:
            spans.append((idxs, None, None, False))

    for pos, (idxs, lo, hi, anchored) in enumerate(spans):
        if anchored:
            continue
        prev_end = next(
            (spans[p][2] for p in range(pos - 1, -1, -1) if spans[p][3]), None
        )
        next_start = next(
            (spans[n][1] for n in range(pos + 1, len(spans)) if spans[n][3]), None
        )
        spans[pos] = (
            idxs,
            0.0 if prev_end is None else prev_end,
            duration if next_start is None else next_start,
            False,
        )

    windows: list[Window] = []
    prev_end = 0.0
    for idxs, lo, hi, anchored in spans:
        start = max(0.0, min(float(lo if lo is not None else 0.0), duration))
        end = max(0.0, min(float(hi if hi is not None else duration), duration))
        start = max(start, prev_end)  # no overlap with the window before it
        if end <= start:
            end = min(duration, start + 0.05)
        windows.append(
            Window(tuple(lines[i] for i in idxs), start, end, anchored)
        )
        prev_end = end
    return _merge_starved(windows)


def _repeated_line_indices(lines: list[str]) -> set[int]:
    """Indices of lines identical to an adjacent line (a repeated refrain)."""
    norm = [
        " ".join(t for t in (normalize_token(w) for w in line.split()) if t)
        for line in lines
    ]
    return {
        i
        for i in range(len(norm))
        if (i > 0 and norm[i] == norm[i - 1])
        or (i + 1 < len(norm) and norm[i] == norm[i + 1])
    }


def _required_s(lines: tuple[str, ...]) -> float:
    """Minimum audio a window needs to hold its text without starving."""
    toks = sum(1 for line in lines for w in line.split() if normalize_token(w))
    return max(MIN_WINDOW_S, toks * MIN_S_PER_TOKEN)


def _merge_starved(windows: list[Window]) -> list[Window]:
    """Fold windows too short for their text into a neighbour.

    A starved window comes from anchors that are close together but wrong (the
    ASR misheard the line, so its few matching tokens cluster). Widening in
    place would overlap the neighbour, so the two are merged and the aligner
    re-splits the combined text over the combined span — which is exactly the
    situation wav2vec2 handles well.
    """
    while len(windows) > 1:
        starved = next(
            (
                i
                for i, w in enumerate(windows)
                if (w.end - w.start) < _required_s(w.lines)
            ),
            None,
        )
        if starved is None:
            break
        neighbours = [i for i in (starved - 1, starved + 1) if 0 <= i < len(windows)]
        # Merge into whichever neighbour has the most slack to spare.
        j = max(
            neighbours,
            key=lambda k: (windows[k].end - windows[k].start)
            - _required_s(windows[k].lines),
        )
        lo, hi = min(starved, j), max(starved, j)
        a, b = windows[lo], windows[hi]
        windows[lo:hi + 1] = [
            Window(
                a.lines + b.lines,
                min(a.start, b.start),
                max(a.end, b.end),
                a.anchored and b.anchored,
            )
        ]
    return windows


def aggregate_words(lines: list[str], words: list[dict]) -> list[dict]:
    """Fold a window's flat word timings back up to one row per line.

    `words` is whisperx's `word_segments` for this window — one entry per token
    of the known text, in order. Never zip against whisperx's `segments`: it
    re-splits the text into its own sentences, so the segment list does not
    correspond to the lines that went in.

    A line whose words all came back untimed (wav2vec2 drops tokens it cannot
    place) yields start/end None rather than a fabricated span.
    """
    rows: list[dict] = []
    cursor = 0
    for line in lines:
        n = sum(1 for w in line.split() if normalize_token(w))
        chunk = words[cursor:cursor + n]
        cursor += n
        timed = [w for w in chunk if w.get("start") is not None]
        if not timed:
            rows.append({"text": line, "start": None, "end": None, "score": 0.0})
            continue
        scores = [float(w.get("score") or 0.0) for w in timed]
        rows.append(
            {
                "text": line,
                "start": round(min(float(w["start"]) for w in timed), 3),
                "end": round(max(float(w["end"]) for w in timed), 3),
                "score": round(sum(scores) / len(scores), 3),
            }
        )
    return rows


def tidy_lines(
    rows: list[dict], min_gap: float = 0.05, max_hold: float = 6.0
) -> list[dict]:
    """Clean aggregated lines into caption-ready timings.

    - drops untimed lines (nothing to show)
    - enforces monotonic, non-overlapping spans
    - caps a held final syllable so one line does not sit on screen for a
      whole instrumental passage
    """
    out: list[dict] = []
    for row in rows:
        if row.get("start") is None or row.get("end") is None:
            continue
        start = float(row["start"])
        end = float(row["end"])
        if out and start < out[-1]["end"] + min_gap:
            start = out[-1]["end"] + min_gap
        if end - start > max_hold:
            end = start + max_hold
        if end <= start:
            continue
        out.append({**row, "start": round(start, 3), "end": round(end, 3)})
    return out
