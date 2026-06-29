"""Tests for the pure technical-quality (QC) helpers.

Loads modal/quality.py by path (modal/ is not an importable package). The
module is stdlib-only.
"""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "quality.py"
_spec = importlib.util.spec_from_file_location("lyricsync_quality", _MODULE_PATH)
q = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(q)


def _sample(t, sharpness=200.0, black_frac=0.0, white_frac=0.0, shake=2.0):
    return {"t": t, "sharpness": sharpness, "black_frac": black_frac,
            "white_frac": white_frac, "shake": shake}


class TestBucketBySecond:
    def test_averages_samples_within_a_second(self):
        samples = [_sample(0.1, sharpness=100.0), _sample(0.6, sharpness=200.0)]
        buckets = q.bucket_by_second(samples, duration=1.0)
        assert len(buckets) == 1
        assert buckets[0]["t"] == 0
        assert buckets[0]["sharpness"] == 150.0

    def test_missing_seconds_are_omitted(self):
        samples = [_sample(0.0), _sample(5.0)]
        buckets = q.bucket_by_second(samples, duration=6.0)
        assert [b["t"] for b in buckets] == [0, 5]

    def test_empty_samples(self):
        assert q.bucket_by_second([], duration=3.0) == []


class TestFlagReasons:
    def test_clean_bucket_has_no_reasons(self):
        b = _sample(0)
        assert q.flag_reasons(b) == []

    def test_soft_flag(self):
        b = _sample(0, sharpness=5.0)
        assert "soft" in q.flag_reasons(b)

    def test_blown_flag(self):
        b = _sample(0, white_frac=0.95)
        assert "blown" in q.flag_reasons(b)

    def test_crushed_flag(self):
        b = _sample(0, black_frac=0.95)
        assert "crushed" in q.flag_reasons(b)

    def test_shake_flag(self):
        b = _sample(0, shake=20.0)
        assert "shake" in q.flag_reasons(b)

    def test_frozen_flag(self):
        b = _sample(0, shake=0.05)
        assert "frozen" in q.flag_reasons(b)


class TestUsableScore:
    def test_no_reasons_is_fully_usable(self):
        assert q.usable_score([]) == 1.0

    def test_one_reason_partial_penalty(self):
        assert q.usable_score(["soft"]) == 0.6

    def test_two_reasons_stack(self):
        assert q.usable_score(["soft", "shake"]) == 0.2

    def test_frozen_is_full_disqualifier(self):
        assert q.usable_score(["frozen"]) == 0.0

    def test_black_is_full_disqualifier(self):
        assert q.usable_score(["black"]) == 0.0


class TestApplyForcedSpans:
    def test_marks_existing_seconds_unusable(self):
        seconds = q.score_seconds(q.bucket_by_second(
            [_sample(0), _sample(1), _sample(2)], duration=3.0,
        ))
        q.apply_forced_spans(seconds, [{"start": 1.0, "end": 2.0}], "black")
        by_t = {s["t"]: s for s in seconds}
        assert by_t[1]["usable"] == 0.0
        assert by_t[1]["reasons"] == ["black"]
        assert by_t[0]["usable"] == 1.0

    def test_inserts_stub_for_missing_second(self):
        seconds = q.score_seconds(q.bucket_by_second([_sample(0)], duration=3.0))
        q.apply_forced_spans(seconds, [{"start": 2.0, "end": 3.0}], "black")
        assert len(seconds) == 2
        assert seconds[1]["t"] == 2
        assert seconds[1]["reasons"] == ["black"]


class TestMergeFlaggedSpans:
    def test_no_flags_no_spans(self):
        seconds = q.score_seconds(q.bucket_by_second(
            [_sample(0), _sample(1)], duration=2.0,
        ))
        assert q.merge_flagged_spans(seconds) == []

    def test_contiguous_flagged_seconds_merge(self):
        samples = [_sample(0, sharpness=5.0), _sample(1, sharpness=5.0), _sample(2)]
        seconds = q.score_seconds(q.bucket_by_second(samples, duration=3.0))
        spans = q.merge_flagged_spans(seconds)
        assert spans == [{"start": 0, "end": 2, "reasons": ["soft"]}]

    def test_gap_splits_spans(self):
        samples = [
            _sample(0, sharpness=5.0), _sample(1), _sample(2, sharpness=5.0),
        ]
        seconds = q.score_seconds(q.bucket_by_second(samples, duration=3.0))
        spans = q.merge_flagged_spans(seconds)
        assert len(spans) == 2
        assert spans[0]["start"] == 0
        assert spans[1]["start"] == 2


class TestSummarize:
    def test_clean_clip_summary(self):
        seconds = q.score_seconds(q.bucket_by_second(
            [_sample(0), _sample(1)], duration=2.0,
        ))
        summary = q.summarize(seconds, [], duration=2.0)
        assert summary["mean_usable"] == 1.0
        assert summary["flagged_seconds"] == 0
        assert summary["reasons_count"] == {}

    def test_flagged_fraction(self):
        samples = [_sample(0, sharpness=5.0), _sample(1)]
        seconds = q.score_seconds(q.bucket_by_second(samples, duration=2.0))
        spans = q.merge_flagged_spans(seconds)
        summary = q.summarize(seconds, spans, duration=2.0)
        assert summary["flagged_seconds"] == 1
        assert summary["flagged_fraction"] == 0.5
        assert summary["reasons_count"] == {"soft": 1}


class TestBuildQualityDoc:
    def test_full_doc_shape(self):
        samples = [_sample(0), _sample(1, sharpness=5.0)]
        doc = q.build_quality_doc(duration=2.0, fps_sampled=3.0, samples=samples)
        assert doc["version"] == q.QUALITY_VERSION
        assert doc["duration"] == 2.0
        assert doc["fps_sampled"] == 3.0
        assert len(doc["seconds"]) == 2
        assert doc["flagged_spans"] == [{"start": 1, "end": 2, "reasons": ["soft"]}]
        assert doc["summary"]["flagged_seconds"] == 1

    def test_black_spans_force_unusable(self):
        samples = [_sample(0), _sample(1)]
        doc = q.build_quality_doc(
            duration=2.0, fps_sampled=3.0, samples=samples,
            black_spans=[{"start": 1.0, "end": 2.0}],
        )
        by_t = {s["t"]: s for s in doc["seconds"]}
        assert by_t[1]["usable"] == 0.0
        assert "black" in doc["summary"]["reasons_count"]


class TestParseBlackdetect:
    def test_parses_single_span(self):
        stderr = (
            "[blackdetect @ 0x55f] black_start:12.34 black_end:15.67 "
            "black_duration:3.33\n"
        )
        assert q.parse_blackdetect(stderr) == [{"start": 12.34, "end": 15.67}]

    def test_parses_multiple_spans(self):
        stderr = (
            "[blackdetect @ 0x1] black_start:0.0 black_end:1.5 black_duration:1.5\n"
            "[blackdetect @ 0x1] black_start:10.0 black_end:12.0 black_duration:2.0\n"
        )
        assert q.parse_blackdetect(stderr) == [
            {"start": 0.0, "end": 1.5}, {"start": 10.0, "end": 12.0},
        ]

    def test_no_match_returns_empty(self):
        assert q.parse_blackdetect("no blackdetect output here") == []

    def test_empty_input(self):
        assert q.parse_blackdetect("") == []
