"""Tests for plan_speech_cleanup — the pure dead-air / filler planner.

Loads modal/timeline.py by path (modal/ is not an importable package and the
name collides with the pip `modal` package). The module is stdlib-only.
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "timeline.py"
_spec = importlib.util.spec_from_file_location("lyricsync_timeline", _MODULE_PATH)
tl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tl)


def w(text, start, end, score=None):
    """Build an aligned word in source-clip seconds."""
    d = {"text": text, "start": start, "end": end}
    if score is not None:
        d["score"] = score
    return d


def total_kept(plan):
    return round(sum(s["end"] - s["start"] for s in plan["keep"]), 3)


# ---------------------------------------------------------------------------
# Degenerate / non-speech cases — the "don't touch intentional silence" rule
# ---------------------------------------------------------------------------

class TestNonSpeech:
    def test_no_words_keeps_whole_span(self):
        plan = tl.plan_speech_cleanup([], 0.0, 5.0)
        assert plan["keep"] == [{"start": 0.0, "end": 5.0}]
        assert plan["removed"] == []
        assert plan["saved"] == 0.0
        assert plan["kept_words"] == 0

    def test_only_fillers_left_untouched(self):
        # All words are fillers -> treated as non-speech, span kept whole.
        words = [w("um", 1.0, 1.3), w("uh", 2.0, 2.2)]
        plan = tl.plan_speech_cleanup(words, 0.0, 4.0)
        assert plan["keep"] == [{"start": 0.0, "end": 4.0}]
        assert plan["saved"] == 0.0
        assert plan["filler_words"] == 0  # only counted when speech remains

    def test_words_outside_span_ignored(self):
        words = [w("hello", 10.0, 10.4), w("there", 10.5, 10.9)]
        plan = tl.plan_speech_cleanup(words, 0.0, 5.0)
        assert plan["keep"] == [{"start": 0.0, "end": 5.0}]


# ---------------------------------------------------------------------------
# Silence collapsing
# ---------------------------------------------------------------------------

class TestSilence:
    def test_short_gap_kept_continuous(self):
        # 0.2 s gap < default max_gap (0.35) -> single continuous keep span.
        words = [w("a", 1.0, 1.3), w("b", 1.5, 1.8)]
        plan = tl.plan_speech_cleanup(words, 0.0, 3.0, {"trim_lead": False,
                                                        "trim_tail": False})
        assert plan["keep"] == [{"start": 0.0, "end": 3.0}]
        assert plan["removed"] == []

    def test_long_gap_collapsed_to_breath(self):
        # 2.0 s gap between words; collapse_to=0.15 keeps a breath, cuts rest.
        words = [w("a", 1.0, 1.4), w("b", 3.4, 3.8)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 5.0,
            {"trim_lead": False, "trim_tail": False,
             "max_gap": 0.35, "collapse_to": 0.15, "pad_start": 0.0,
             "pad_end": 0.0, "protect_gap_over": None},
        )
        assert len(plan["keep"]) == 2
        # first span ends at word-a end (1.4) + breath (0.15) = 1.55
        assert plan["keep"][0] == {"start": 0.0, "end": 1.55}
        assert plan["keep"][1] == {"start": 3.4, "end": 5.0}
        sil = [r for r in plan["removed"] if r["reason"] == "silence"]
        assert len(sil) == 1
        assert sil[0] == {"start": 1.55, "end": 3.4, "reason": "silence"}

    def test_protected_long_gap_not_cut(self):
        # Same 2.0 s gap, but protect_gap_over=1.5 -> left intact.
        words = [w("a", 1.0, 1.4), w("b", 3.4, 3.8)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 5.0,
            {"trim_lead": False, "trim_tail": False, "protect_gap_over": 1.5},
        )
        assert plan["keep"] == [{"start": 0.0, "end": 5.0}]
        assert plan["saved"] == 0.0

    def test_min_removed_skips_tiny_cuts(self):
        # Gap just over max_gap; after keeping collapse_to the leftover is tiny
        # and below min_removed, so no cut is made.
        words = [w("a", 1.0, 1.4), w("b", 1.9, 2.3)]
        # gap 0.5; after keeping collapse_to=0.15 only 0.35 is cuttable, which
        # is below min_removed=0.5 -> left continuous.
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0,
            {"trim_lead": False, "trim_tail": False, "max_gap": 0.3,
             "collapse_to": 0.15, "pad_start": 0.0, "pad_end": 0.0,
             "min_removed": 0.5},
        )
        assert plan["keep"] == [{"start": 0.0, "end": 3.0}]


# ---------------------------------------------------------------------------
# Filler removal
# ---------------------------------------------------------------------------

class TestFillers:
    def test_drops_default_filler(self):
        words = [w("i", 1.0, 1.2), w("um", 1.25, 1.5), w("think", 1.55, 1.9)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0,
            {"trim_lead": False, "trim_tail": False, "pad_start": 0.0,
             "pad_end": 0.0, "max_gap": 0.05, "collapse_to": 0.0,
             "protect_gap_over": None, "min_removed": 0.0},
        )
        assert plan["filler_words"] == 1
        fillers = [r for r in plan["removed"] if r["reason"] == "filler"]
        assert len(fillers) == 1
        assert fillers[0]["text"] == "um"
        # "um" punctuation-insensitive: the kept word "think" survives.
        assert any(s["end"] >= 1.9 for s in plan["keep"])

    def test_filler_matching_is_punctuation_insensitive(self):
        words = [w("a", 1.0, 1.2), w("Um,", 1.25, 1.5), w("b", 1.6, 1.9)]
        plan = tl.plan_speech_cleanup(words, 0.0, 3.0)
        assert plan["filler_words"] == 1

    def test_multi_word_filler_phrase(self):
        words = [
            w("it", 1.0, 1.2), w("you", 1.25, 1.4), w("know", 1.45, 1.7),
            w("works", 1.75, 2.1),
        ]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0,
            {"filler_lexicon": ["you know"], "pad_start": 0.0, "pad_end": 0.0,
             "collapse_to": 0.0, "max_gap": 0.05, "protect_gap_over": None,
             "min_removed": 0.0},
        )
        assert plan["filler_words"] == 2
        fillers = [r for r in plan["removed"] if r["reason"] == "filler"]
        assert any("you know" in r.get("text", "") for r in fillers)

    def test_remove_fillers_disabled(self):
        words = [w("a", 1.0, 1.2), w("um", 1.25, 1.5), w("b", 1.6, 1.9)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0, {"remove_fillers": False},
        )
        assert plan["filler_words"] == 0


# ---------------------------------------------------------------------------
# Padding, confidence, lead/tail trim
# ---------------------------------------------------------------------------

class TestPaddingAndTrim:
    def test_word_padding_applied(self):
        words = [w("hi", 1.0, 1.4)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0,
            {"pad_start": 0.04, "pad_end": 0.06},
        )
        assert plan["keep"] == [{"start": 0.96, "end": 1.46}]

    def test_padding_clamped_to_span(self):
        words = [w("hi", 0.02, 2.99)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0, {"pad_start": 0.1, "pad_end": 0.1},
        )
        assert plan["keep"] == [{"start": 0.0, "end": 3.0}]

    def test_low_score_gets_extra_pad(self):
        words = [w("hi", 1.0, 1.4, score=0.2)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0,
            {"pad_start": 0.04, "pad_end": 0.04, "min_score": 0.5,
             "low_score_pad": 0.1},
        )
        # 0.04 + 0.10 extra pad each side.
        assert plan["keep"] == [{"start": 0.86, "end": 1.54}]

    def test_high_score_no_extra_pad(self):
        words = [w("hi", 1.0, 1.4, score=0.9)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0,
            {"pad_start": 0.04, "pad_end": 0.04, "min_score": 0.5,
             "low_score_pad": 0.1},
        )
        assert plan["keep"] == [{"start": 0.96, "end": 1.44}]

    def test_lead_and_tail_trimmed(self):
        words = [w("hi", 1.5, 1.9)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 4.0,
            {"pad_start": 0.0, "pad_end": 0.0, "trim_lead": True,
             "trim_tail": True},
        )
        assert plan["keep"] == [{"start": 1.5, "end": 1.9}]
        reasons = {r["reason"] for r in plan["removed"]}
        assert reasons == {"lead_silence", "tail_silence"}

    def test_lead_tail_kept_when_disabled(self):
        words = [w("hi", 1.5, 1.9)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 4.0,
            {"pad_start": 0.0, "pad_end": 0.0, "trim_lead": False,
             "trim_tail": False},
        )
        assert plan["keep"] == [{"start": 0.0, "end": 4.0}]


# ---------------------------------------------------------------------------
# Bookkeeping
# ---------------------------------------------------------------------------

class TestBookkeeping:
    def test_durations_and_saved_consistent(self):
        words = [w("a", 0.5, 0.9), w("b", 3.5, 3.9)]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 5.0,
            {"pad_start": 0.0, "pad_end": 0.0, "protect_gap_over": None},
        )
        assert plan["duration_before"] == 5.0
        assert plan["duration_after"] == total_kept(plan)
        assert plan["saved"] == pytest.approx(
            plan["duration_before"] - plan["duration_after"]
        )
        assert plan["saved"] > 0

    def test_accepts_local_time_keys(self):
        words = [{"text": "hi", "local_start": 1.0, "local_end": 1.4}]
        plan = tl.plan_speech_cleanup(
            words, 0.0, 3.0, {"pad_start": 0.0, "pad_end": 0.0},
        )
        assert plan["keep"] == [{"start": 1.0, "end": 1.4}]
