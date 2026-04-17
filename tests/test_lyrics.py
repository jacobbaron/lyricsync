"""Tests for lyrics parsing."""

from lyricsync.lyrics import parse_lyrics_text


def test_simple_parse():
    raw = "when the morning light\ncomes through the window\n"
    doc = parse_lyrics_text(raw)
    assert len(doc.lines) == 2
    assert doc.lines[0].text == "when the morning light"
    assert doc.lines[0].words == ("when", "the", "morning", "light")
    assert doc.lines[1].words == ("comes", "through", "the", "window")
    assert doc.flat_words == [
        "when", "the", "morning", "light",
        "comes", "through", "the", "window",
    ]


def test_blank_lines_are_gaps_not_captions():
    raw = (
        "verse one line one\n"
        "verse one line two\n"
        "\n"
        "chorus line one\n"
        "chorus line two\n"
    )
    doc = parse_lyrics_text(raw)
    # blank lines should NOT become caption lines
    assert len(doc.lines) == 4
    # but we should have marked that a break followed line 1 (index 1)
    assert doc.line_breaks[1] is True
    # line 0 was not followed by a blank
    assert doc.line_breaks[0] is False


def test_strips_whitespace():
    raw = "   padded line   \n\tleading tab\n"
    doc = parse_lyrics_text(raw)
    assert doc.lines[0].text == "padded line"
    assert doc.lines[1].text == "leading tab"


def test_empty_input():
    doc = parse_lyrics_text("")
    assert doc.lines == ()
    assert doc.flat_words == []


def test_only_blank_lines():
    doc = parse_lyrics_text("\n\n\n")
    assert doc.lines == ()
