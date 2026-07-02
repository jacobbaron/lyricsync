"""Tests for alignment_json.py error paths and edge cases.

test_alignment_json.py covers the happy-path round-trip; these tests
exercise validation errors, missing fields, and boundary conditions.
"""

from __future__ import annotations

import pytest

from lyricsync.alignment import AlignedLine, AlignedWord, AlignmentResult
from lyricsync.alignment_json import (
    SCHEMA_VERSION,
    alignment_from_dict,
    alignment_to_dict,
    read_alignment_json,
)


def _valid_dict() -> dict:
    return {
        "schema_version": SCHEMA_VERSION,
        "lines": [
            {
                "text": "hello",
                "words": [{"text": "hello", "start": 0.0, "end": 0.5}],
            }
        ],
    }


class TestAlignmentFromDictErrors:
    def test_wrong_schema_version_raises(self):
        d = _valid_dict()
        d["schema_version"] = 99
        with pytest.raises(ValueError, match="unsupported alignment schema_version"):
            alignment_from_dict(d)

    def test_missing_schema_version_raises(self):
        d = _valid_dict()
        del d["schema_version"]
        with pytest.raises(ValueError, match="unsupported alignment schema_version"):
            alignment_from_dict(d)

    def test_missing_lines_key_raises(self):
        with pytest.raises(ValueError, match="missing 'lines' list"):
            alignment_from_dict({"schema_version": SCHEMA_VERSION})

    def test_lines_not_a_list_raises(self):
        with pytest.raises(ValueError, match="missing 'lines' list"):
            alignment_from_dict({"schema_version": SCHEMA_VERSION, "lines": "bad"})

    def test_line_not_a_dict_raises(self):
        d = {"schema_version": SCHEMA_VERSION, "lines": ["not a dict"]}
        with pytest.raises(ValueError, match="lines\\[0\\] must be an object"):
            alignment_from_dict(d)

    def test_line_missing_text_raises(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [{"words": [{"text": "a", "start": 0, "end": 1}]}],
        }
        with pytest.raises(ValueError, match="lines\\[0\\] needs string 'text'"):
            alignment_from_dict(d)

    def test_line_missing_words_raises(self):
        d = {"schema_version": SCHEMA_VERSION, "lines": [{"text": "hi"}]}
        with pytest.raises(ValueError, match="lines\\[0\\] needs string 'text' and list 'words'"):
            alignment_from_dict(d)

    def test_word_not_a_dict_raises(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [{"text": "hi", "words": ["bad"]}],
        }
        with pytest.raises(ValueError, match="words\\[0\\] must be an object"):
            alignment_from_dict(d)

    def test_word_missing_start_raises(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [{"text": "hi", "words": [{"text": "hi", "end": 1.0}]}],
        }
        with pytest.raises(ValueError, match="needs text and numeric start"):
            alignment_from_dict(d)

    def test_word_missing_end_raises(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [{"text": "hi", "words": [{"text": "hi", "start": 0.0}]}],
        }
        with pytest.raises(ValueError, match="needs numeric end"):
            alignment_from_dict(d)

    def test_word_string_start_raises(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [
                {"text": "hi", "words": [{"text": "hi", "start": "zero", "end": 1.0}]}
            ],
        }
        with pytest.raises(ValueError, match="needs text and numeric start"):
            alignment_from_dict(d)

    def test_empty_words_line_is_skipped(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [
                {"text": "empty line", "words": []},
                {
                    "text": "real line",
                    "words": [{"text": "real", "start": 0.0, "end": 1.0}],
                },
            ],
        }
        result = alignment_from_dict(d)
        assert len(result.lines) == 1
        assert result.lines[0].text == "real line"

    def test_end_before_start_clamped(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [
                {
                    "text": "word",
                    "words": [{"text": "word", "start": 5.0, "end": 2.0}],
                }
            ],
        }
        result = alignment_from_dict(d)
        w = result.lines[0].words[0]
        assert w.end >= w.start  # end clamped to start

    def test_integer_times_accepted(self):
        d = {
            "schema_version": SCHEMA_VERSION,
            "lines": [
                {
                    "text": "word",
                    "words": [{"text": "word", "start": 0, "end": 1}],
                }
            ],
        }
        result = alignment_from_dict(d)
        assert result.lines[0].words[0].start == 0.0
        assert result.lines[0].words[0].end == 1.0


class TestAlignmentToDict:
    def test_includes_meta_when_provided(self):
        r = AlignmentResult(lines=())
        d = alignment_to_dict(r, meta={"key": "value"})
        assert d["meta"] == {"key": "value"}

    def test_no_meta_key_when_none(self):
        r = AlignmentResult(lines=())
        d = alignment_to_dict(r)
        assert "meta" not in d

    def test_empty_meta_not_included(self):
        r = AlignmentResult(lines=())
        d = alignment_to_dict(r, meta={})
        assert "meta" not in d

    def test_schema_version_present(self):
        r = AlignmentResult(lines=())
        d = alignment_to_dict(r)
        assert d["schema_version"] == SCHEMA_VERSION


class TestReadAlignmentJson:
    def test_non_object_root_raises(self, tmp_path):
        path = tmp_path / "bad.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ValueError, match="root must be an object"):
            read_alignment_json(path)
