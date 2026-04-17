"""Tests for SRT formatting.

Uses a synthetic AlignmentResult so we never touch WhisperX or audio.
"""

from lyricsync.alignment import (
    AlignedLine,
    AlignedWord,
    AlignmentResult,
    aggregate_words_to_lines,
)
from lyricsync.lyrics import parse_lyrics_text
from lyricsync.srt import build_srt, format_timestamp


def test_format_timestamp_zero():
    assert format_timestamp(0.0) == "00:00:00,000"


def test_format_timestamp_sub_second():
    assert format_timestamp(0.123) == "00:00:00,123"


def test_format_timestamp_hours_minutes():
    # 1h 2m 3.456s
    secs = 3600 + 2 * 60 + 3.456
    assert format_timestamp(secs) == "01:02:03,456"


def test_format_timestamp_truncates_not_rounds():
    # 0.9999s should truncate to 999ms, not round up to next second
    assert format_timestamp(0.9999) == "00:00:00,999"


def test_format_timestamp_clamps_negative():
    assert format_timestamp(-5.0) == "00:00:00,000"


def test_build_srt_basic():
    result = AlignmentResult(
        lines=(
            AlignedLine(
                text="hello world",
                start=1.0,
                end=2.5,
                words=(
                    AlignedWord("hello", 1.0, 1.7),
                    AlignedWord("world", 1.7, 2.5),
                ),
            ),
            AlignedLine(
                text="line two",
                start=3.0,
                end=4.0,
                words=(
                    AlignedWord("line", 3.0, 3.5),
                    AlignedWord("two", 3.5, 4.0),
                ),
            ),
        )
    )
    out = build_srt(result)
    assert "1\n00:00:01,000 --> 00:00:02,500\nhello world" in out
    assert "2\n00:00:03,000 --> 00:00:04,000\nline two" in out


def test_aggregate_words_to_lines_from_synthetic_alignment():
    # Synthetic "alignment.json"-shaped word_timings: the flat list
    # WhisperX's align() puts under result["word_segments"].
    lyrics = parse_lyrics_text("hello world\nfoo bar baz\n")
    word_timings = [
        {"word": "hello", "start": 0.0, "end": 0.5},
        {"word": "world", "start": 0.5, "end": 1.0},
        {"word": "foo", "start": 2.0, "end": 2.3},
        {"word": "bar", "start": 2.3, "end": 2.6},
        {"word": "baz", "start": 2.6, "end": 3.0},
    ]
    result = aggregate_words_to_lines(lyrics, word_timings)

    assert len(result.lines) == 2
    assert result.lines[0].text == "hello world"
    assert result.lines[0].start == 0.0
    assert result.lines[0].end == 1.0
    assert result.lines[1].text == "foo bar baz"
    assert result.lines[1].start == 2.0
    assert result.lines[1].end == 3.0


def test_aggregate_fills_missing_timestamps():
    lyrics = parse_lyrics_text("a b c\n")
    # middle word has no timing — should interpolate from neighbors
    word_timings = [
        {"word": "a", "start": 1.0, "end": 1.2},
        {"word": "b"},  # no start/end
        {"word": "c", "start": 2.0, "end": 2.3},
    ]
    result = aggregate_words_to_lines(lyrics, word_timings)
    line = result.lines[0]
    assert line.start == 1.0
    assert line.end == 2.3
    # the missing middle word should have been filled with something
    # non-None and monotonic.
    mid = line.words[1]
    assert 1.0 <= mid.start <= 2.0


def test_aggregate_raises_on_word_count_mismatch():
    import pytest

    lyrics = parse_lyrics_text("a b c\n")
    with pytest.raises(ValueError):
        aggregate_words_to_lines(lyrics, [{"word": "a", "start": 0, "end": 1}])
