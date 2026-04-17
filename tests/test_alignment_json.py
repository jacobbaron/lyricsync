"""Tests for alignment.json round-trip."""

from lyricsync.alignment import AlignedLine, AlignedWord, AlignmentResult
from lyricsync.alignment_json import (
    alignment_from_dict,
    alignment_to_dict,
    read_alignment_json,
    write_alignment_json,
)


def _sample_result() -> AlignmentResult:
    return AlignmentResult(
        lines=(
            AlignedLine(
                text="hello world",
                start=0.0,
                end=1.0,
                words=(
                    AlignedWord(text="hello", start=0.0, end=0.5),
                    AlignedWord(text="world", start=0.5, end=1.0),
                ),
            ),
        )
    )


def test_round_trip_dict():
    original = _sample_result()
    d = alignment_to_dict(original, meta={"source_video": "a.mp4"})
    restored = alignment_from_dict(d)
    assert restored == original
    assert d["meta"]["source_video"] == "a.mp4"


def test_recomputes_line_bounds_from_words():
    d = alignment_to_dict(_sample_result())
    d["lines"][0]["start"] = 99.0
    d["lines"][0]["end"] = 100.0
    restored = alignment_from_dict(d)
    assert restored.lines[0].start == 0.0
    assert restored.lines[0].end == 1.0


def test_write_read_round_trip(tmp_path):
    path = tmp_path / "alignment.json"
    original = _sample_result()
    write_alignment_json(original, path)
    assert read_alignment_json(path) == original


def test_clamps_negative_word_times():
    d = alignment_to_dict(_sample_result())
    d["lines"][0]["words"][0]["start"] = -1.0
    d["lines"][0]["words"][0]["end"] = -0.5
    restored = alignment_from_dict(d)
    assert restored.lines[0].words[0].start == 0.0
    assert restored.lines[0].words[0].end == 0.0
