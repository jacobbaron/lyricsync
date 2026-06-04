"""Unit tests for modal/visual.py — timestamp normalization + response parsing.

Pure stdlib, mirrors tests/test_transcript_match.py. No Gemini/Modal/network.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

# Put modal/ on the path so `import visual` resolves to modal/visual.py.
_MODAL_DIR = Path(__file__).resolve().parent.parent / "modal"
if str(_MODAL_DIR) not in sys.path:
    sys.path.insert(0, str(_MODAL_DIR))

import visual  # noqa: E402


# ── normalize_timestamp ───────────────────────────────────────────────────────

@pytest.mark.parametrize(
    "value,expected",
    [
        (12.5, 12.5),
        (0, 0.0),
        ("12.5", 12.5),
        ("12.5s", 12.5),
        ("  3 s ", 3.0),
        ("0:30", 30.0),
        ("1:05", 65.0),
        ("01:01:01", 3661.0),
    ],
)
def test_normalize_timestamp_ok(value, expected):
    assert visual.normalize_timestamp(value) == pytest.approx(expected)


@pytest.mark.parametrize(
    "value",
    [None, "", "abc", "1:2:3:4:bad", -5, "-5", True, False, float("inf"), float("nan")],
)
def test_normalize_timestamp_rejects(value):
    assert visual.normalize_timestamp(value) is None


# ── parse_visual_response ──────────────────────────────────────────────────────

def test_parse_basic():
    raw = json.dumps({
        "summary": "Two people at a table.",
        "segments": [
            {"start": 0, "end": 8.4, "shot": "Wide", "description": "wide shot"},
            {"start": 8.4, "end": 15.2, "shot": "medium", "description": "lean in"},
        ],
        "highlights": [
            {"time": 12.6, "kind": "Reaction", "description": "laugh"},
        ],
    })
    out = visual.parse_visual_response(raw)
    assert out["summary"] == "Two people at a table."
    assert len(out["segments"]) == 2
    assert out["segments"][0]["shot"] == "wide"  # lowercased
    assert out["highlights"][0]["kind"] == "reaction"
    assert out["suggested_clips"] == []


def test_parse_strips_code_fence_and_clock_timestamps():
    raw = (
        "```json\n"
        + json.dumps({
            "summary": "x",
            "segments": [{"start": "0:00", "end": "0:10", "description": "a"}],
            "highlights": [],
        })
        + "\n```"
    )
    out = visual.parse_visual_response(raw)
    assert out["segments"][0]["start"] == 0.0
    assert out["segments"][0]["end"] == 10.0


def test_parse_drops_invalid_and_clamps_to_duration():
    raw = json.dumps({
        "segments": [
            {"start": 5, "end": 3, "description": "backwards — dropped"},
            {"start": 0, "end": 999, "description": "clamped"},
            {"start": "bad", "end": 4, "description": "unparseable start — dropped"},
        ],
        "highlights": [{"time": 999, "kind": "action", "description": "clamped"}],
    })
    out = visual.parse_visual_response(raw, duration_secs=20.0)
    assert len(out["segments"]) == 1
    assert out["segments"][0]["end"] == 20.0
    assert out["highlights"][0]["time"] == 20.0


def test_parse_editorial_suggested_clips():
    raw = json.dumps({
        "summary": "s",
        "segments": [],
        "highlights": [],
        "suggested_clips": [
            {"start": 2, "end": 10, "reason": "good moment"},
            {"start": 1, "end": 0.5, "reason": "invalid — dropped"},
        ],
    })
    out = visual.parse_visual_response(raw)
    assert len(out["suggested_clips"]) == 1
    assert out["suggested_clips"][0]["start"] == 2.0
    assert out["suggested_clips"][0]["reason"] == "good moment"


def test_parse_rejects_non_json():
    with pytest.raises(ValueError):
        visual.parse_visual_response("not json at all")


def test_build_prompt_editorial_mentions_suggested_clips():
    assert "suggested_clips" in visual.build_prompt(30.0, strategy="editorial")
    assert "suggested_clips" not in visual.build_prompt(30.0, strategy="default")
