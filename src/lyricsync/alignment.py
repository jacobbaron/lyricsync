"""Alignment data model + aggregation.

v0 keeps this deliberately simple: plain dataclasses, no pydantic, no
JSON schema. v1 will promote this to the canonical `alignment.json`
intermediate described in spec §6.4.
"""

from __future__ import annotations

from dataclasses import dataclass

from .lyrics import LyricsDoc


@dataclass(frozen=True)
class AlignedWord:
    text: str
    start: float  # seconds
    end: float


@dataclass(frozen=True)
class AlignedLine:
    text: str
    start: float
    end: float
    words: tuple[AlignedWord, ...]


@dataclass(frozen=True)
class AlignmentResult:
    lines: tuple[AlignedLine, ...]


def aggregate_words_to_lines(
    lyrics: LyricsDoc,
    word_timings: list[dict],
) -> AlignmentResult:
    """Aggregate WhisperX word-level output back up to line granularity.

    ``word_timings`` is the shape WhisperX's ``align()`` returns inside
    ``result["word_segments"]``: a flat list of dicts with keys
    ``"word"``, ``"start"``, ``"end"`` (some entries may be missing
    start/end if alignment failed on that token; we fill forward from
    neighbors).

    We rely on positional correspondence: the i-th aligned word maps to
    the i-th word in ``lyrics.flat_words``. WhisperX preserves input
    order, so this holds as long as the caller fed the same flattened
    transcript into align().
    """
    flat = lyrics.flat_words
    if len(word_timings) != len(flat):
        raise ValueError(
            f"word count mismatch: lyrics has {len(flat)} words, "
            f"aligner returned {len(word_timings)}"
        )

    # fill forward/back for any missing timestamps
    cleaned = _fill_missing_timestamps(word_timings)

    aligned_words = [
        AlignedWord(text=flat[i], start=cleaned[i][0], end=cleaned[i][1])
        for i in range(len(flat))
    ]

    # walk lyrics line-by-line, consuming words positionally
    aligned_lines: list[AlignedLine] = []
    cursor = 0
    for line in lyrics.lines:
        n = len(line.words)
        chunk = tuple(aligned_words[cursor : cursor + n])
        cursor += n
        if not chunk:
            continue
        aligned_lines.append(
            AlignedLine(
                text=line.text,
                start=chunk[0].start,
                end=chunk[-1].end,
                words=chunk,
            )
        )

    return AlignmentResult(lines=tuple(aligned_lines))


def _fill_missing_timestamps(
    word_timings: list[dict],
) -> list[tuple[float, float]]:
    """Return a parallel list of (start, end) with no Nones.

    WhisperX occasionally returns entries without start/end when it
    couldn't align that token (punctuation, OOV). We linearly interpolate
    between the surrounding anchored words to keep downstream code
    simple.
    """
    raw: list[tuple[float | None, float | None]] = [
        (w.get("start"), w.get("end")) for w in word_timings
    ]

    # forward-fill starts from the previous end
    last_known: float | None = None
    for i, (s, _e) in enumerate(raw):
        if s is not None:
            last_known = s
        elif last_known is not None:
            raw[i] = (last_known, raw[i][1])

    # backward-fill any still-None starts
    next_known: float | None = None
    for i in range(len(raw) - 1, -1, -1):
        s, e = raw[i]
        if s is not None:
            next_known = s
        elif next_known is not None:
            raw[i] = (next_known, e)

    # fill ends: default to next start, or to own start if last word
    out: list[tuple[float, float]] = []
    for i, (s, e) in enumerate(raw):
        s_val = s if s is not None else 0.0
        if e is not None:
            e_val = e
        elif i + 1 < len(raw) and raw[i + 1][0] is not None:
            e_val = raw[i + 1][0]  # type: ignore[assignment]
        else:
            e_val = s_val
        out.append((float(s_val), float(e_val)))
    return out
