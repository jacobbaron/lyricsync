"""Tests for alignment.py — aggregate_words_to_lines and _fill_missing_timestamps.

test_srt.py covers the basic happy path; these tests target edge cases
in timestamp filling and multi-line aggregation with blank-line gaps.
"""

from __future__ import annotations

import pytest

from lyricsync.alignment import (
    AlignmentResult,
    aggregate_words_to_lines,
    _fill_missing_timestamps,
)
from lyricsync.lyrics import parse_lyrics_text


# ---------------------------------------------------------------------------
# _fill_missing_timestamps
# ---------------------------------------------------------------------------


class TestFillMissingTimestamps:
    def test_all_present_unchanged(self):
        timings = [
            {"start": 0.0, "end": 0.5},
            {"start": 0.5, "end": 1.0},
        ]
        result = _fill_missing_timestamps(timings)
        assert result == [(0.0, 0.5), (0.5, 1.0)]

    def test_missing_start_filled_forward(self):
        timings = [
            {"start": 1.0, "end": 1.5},
            {"end": 2.0},  # missing start
        ]
        result = _fill_missing_timestamps(timings)
        assert result[1][0] == 1.0  # forward-filled from previous start

    def test_missing_start_filled_backward(self):
        timings = [
            {"end": 0.5},  # missing start, no previous
            {"start": 1.0, "end": 1.5},
        ]
        result = _fill_missing_timestamps(timings)
        assert result[0][0] == 1.0  # backward-filled from next start

    def test_missing_end_filled_from_next_start(self):
        timings = [
            {"start": 0.0},  # missing end
            {"start": 0.5, "end": 1.0},
        ]
        result = _fill_missing_timestamps(timings)
        assert result[0][1] == 0.5  # next word's start

    def test_missing_end_last_word_uses_own_start(self):
        timings = [
            {"start": 0.0, "end": 0.5},
            {"start": 1.0},  # missing end, last word
        ]
        result = _fill_missing_timestamps(timings)
        assert result[1][1] == 1.0  # falls back to own start

    def test_all_missing_defaults_to_zero(self):
        timings = [{}]
        result = _fill_missing_timestamps(timings)
        assert result == [(0.0, 0.0)]

    def test_single_word_all_present(self):
        result = _fill_missing_timestamps([{"start": 2.0, "end": 3.0}])
        assert result == [(2.0, 3.0)]

    def test_empty_list(self):
        assert _fill_missing_timestamps([]) == []

    def test_consecutive_missing_starts(self):
        timings = [
            {"start": 0.0, "end": 0.5},
            {"end": 1.0},  # missing start
            {"end": 1.5},  # missing start
            {"start": 2.0, "end": 2.5},
        ]
        result = _fill_missing_timestamps(timings)
        # both should forward-fill from the first word's start
        assert result[1][0] == 0.0
        assert result[2][0] == 0.0


# ---------------------------------------------------------------------------
# aggregate_words_to_lines
# ---------------------------------------------------------------------------


class TestAggregateWordsToLines:
    def test_skips_blank_lines_in_lyrics(self):
        lyrics = parse_lyrics_text("hello world\n\nfoo bar\n")
        word_timings = [
            {"word": "hello", "start": 0.0, "end": 0.5},
            {"word": "world", "start": 0.5, "end": 1.0},
            {"word": "foo", "start": 2.0, "end": 2.5},
            {"word": "bar", "start": 2.5, "end": 3.0},
        ]
        result = aggregate_words_to_lines(lyrics, word_timings)
        assert len(result.lines) == 2
        assert result.lines[0].text == "hello world"
        assert result.lines[1].text == "foo bar"

    def test_single_word_line(self):
        lyrics = parse_lyrics_text("hey\n")
        word_timings = [{"word": "hey", "start": 0.0, "end": 0.5}]
        result = aggregate_words_to_lines(lyrics, word_timings)
        assert len(result.lines) == 1
        assert result.lines[0].words == result.lines[0].words
        assert result.lines[0].start == 0.0
        assert result.lines[0].end == 0.5

    def test_line_start_end_from_first_last_word(self):
        lyrics = parse_lyrics_text("a b c\n")
        word_timings = [
            {"word": "a", "start": 1.0, "end": 1.3},
            {"word": "b", "start": 1.5, "end": 1.8},
            {"word": "c", "start": 2.0, "end": 2.5},
        ]
        result = aggregate_words_to_lines(lyrics, word_timings)
        assert result.lines[0].start == 1.0
        assert result.lines[0].end == 2.5

    def test_word_count_mismatch_raises(self):
        lyrics = parse_lyrics_text("a b\n")
        with pytest.raises(ValueError, match="word count mismatch"):
            aggregate_words_to_lines(
                lyrics, [{"word": "a", "start": 0, "end": 1}]
            )

    def test_result_is_frozen_dataclass(self):
        lyrics = parse_lyrics_text("hi\n")
        word_timings = [{"word": "hi", "start": 0.0, "end": 0.5}]
        result = aggregate_words_to_lines(lyrics, word_timings)
        assert isinstance(result, AlignmentResult)
        with pytest.raises(AttributeError):
            result.lines = ()  # type: ignore[misc]

    def test_multiple_lines_with_gaps_between(self):
        lyrics = parse_lyrics_text("first\n\nsecond\n\nthird\n")
        word_timings = [
            {"word": "first", "start": 0.0, "end": 0.5},
            {"word": "second", "start": 2.0, "end": 2.5},
            {"word": "third", "start": 4.0, "end": 4.5},
        ]
        result = aggregate_words_to_lines(lyrics, word_timings)
        assert len(result.lines) == 3
        assert result.lines[2].text == "third"
        assert result.lines[2].start == 4.0
