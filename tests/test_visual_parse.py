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


# ── format_signals_prompt (PERCEPTION T3 grounding block) ──────────────────────

_MOTION = {
    "summary": {"dominant": "pan"},
    "spans": [
        {"start": 0, "end": 4, "label": "static"},
        {"start": 4, "end": 9, "label": "pan"},
        {"start": 9, "end": 12, "label": "zoom_in"},
    ],
    "scene_cuts": [12.0, 25.5],
}
_QUALITY = {
    "summary": {"mean_usable": 0.8},
    "flagged_spans": [
        {"start": 2, "end": 3, "reasons": ["soft"]},
        {"start": 10, "end": 12, "reasons": ["black", "crushed"]},
    ],
}


def test_format_signals_empty_when_none():
    assert visual.format_signals_prompt(None) == ""
    assert visual.format_signals_prompt({}) == ""


def test_format_signals_renders_motion_spans_and_cuts():
    block = visual.format_signals_prompt({"camera_motion": _MOTION})
    assert "0-4s static" in block
    assert "4-9s pan" in block
    assert "9-12s zoom_in" in block
    assert "In-camera shot cuts at: 12s, 25.5s." in block


def test_format_signals_renders_quality_flags():
    block = visual.format_signals_prompt({"quality": _QUALITY})
    assert "2-3s soft" in block
    assert "10-12s black/crushed" in block
    assert "avoid cutting here" in block


def test_format_signals_quality_clean_when_no_flags():
    block = visual.format_signals_prompt(
        {"quality": {"summary": {}, "flagged_spans": []}}
    )
    assert "no unusable spans detected" in block


def test_format_secs_drops_trailing_zero():
    assert visual._fmt_secs(4) == "4"
    assert visual._fmt_secs(4.0) == "4"
    assert visual._fmt_secs(12.5) == "12.5"


def test_build_prompt_embeds_grounding_block_when_signals_given():
    grounded = visual.build_prompt(
        30.0, strategy="transcript",
        signals={"camera_motion": _MOTION, "quality": _QUALITY},
    )
    assert "GROUND TRUTH" in grounded
    assert "Camera motion over time" in grounded
    assert "4-9s pan" in grounded
    assert "Technically unusable spans" in grounded


def test_build_prompt_unchanged_without_signals():
    # Ungrounded prompt must be byte-for-byte identical to passing signals=None.
    assert (
        visual.build_prompt(30.0, strategy="transcript")
        == visual.build_prompt(30.0, strategy="transcript", signals=None)
    )
    assert "GROUND TRUTH" not in visual.build_prompt(30.0, strategy="transcript")
