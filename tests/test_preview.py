"""Tests for the drawtext filter string construction.

We don't invoke ffmpeg here — just verify the filter string we build
is well-formed and escapes correctly.
"""

from lyricsync.alignment import AlignedLine, AlignedWord, AlignmentResult
from lyricsync.preview import build_drawtext_filter, escape_drawtext


def _line(text: str, start: float, end: float) -> AlignedLine:
    # Minimal AlignedLine; v0's drawtext builder ignores the words.
    return AlignedLine(
        text=text,
        start=start,
        end=end,
        words=(AlignedWord(text, start, end),),
    )


def test_escape_handles_special_chars():
    assert escape_drawtext("plain") == "plain"
    assert escape_drawtext("1, 2") == r"1\, 2"
    assert escape_drawtext("a:b") == r"a\:b"
    assert escape_drawtext("50%") == r"50\%"
    assert escape_drawtext("it's") == r"it\'s"
    assert escape_drawtext("back\\slash") == r"back\\slash"


def test_empty_result_returns_null_filter():
    # drawtext with no lines should be a no-op passthrough.
    assert build_drawtext_filter(AlignmentResult(lines=())) == "null"


def test_filter_contains_one_entry_per_line():
    result = AlignmentResult(
        lines=(
            _line("first", 1.0, 2.0),
            _line("second", 3.0, 4.5),
        )
    )
    vf = build_drawtext_filter(result)
    # chained with commas — ffmpeg's filter separator
    assert vf.count("drawtext=") == 2
    # each line's time window shows up in its enable= clause
    assert r"between(t\,1.000\,2.000)" in vf
    assert r"between(t\,3.000\,4.500)" in vf
    # text values are quoted
    assert "text='first'" in vf
    assert "text='second'" in vf


def test_filter_escapes_apostrophes_in_text():
    result = AlignmentResult(lines=(_line("don't stop", 0.0, 1.0),))
    vf = build_drawtext_filter(result)
    # single quote must be backslash-escaped inside the quoted value
    assert r"\'" in vf
    # and the outer quoting is still balanced
    assert vf.startswith("drawtext=text='")


def test_filter_escapes_colons_in_text():
    # colons would otherwise split the drawtext options list
    result = AlignmentResult(lines=(_line("1:23 a.m.", 0.0, 1.0),))
    vf = build_drawtext_filter(result)
    assert r"1\:23" in vf


def test_filter_escapes_commas_in_text():
    # commas would otherwise split chained filters unexpectedly
    result = AlignmentResult(lines=(_line("1, 2, 3, 4!", 0.0, 1.0),))
    vf = build_drawtext_filter(result)
    assert r"1\, 2\, 3\, 4!" in vf
