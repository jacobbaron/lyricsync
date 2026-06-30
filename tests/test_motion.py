"""Tests for the pure camera-motion (shot-dynamics) helpers.

Loads modal/motion.py by path (modal/ is not an importable package). The
module is stdlib-only.
"""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "motion.py"
_spec = importlib.util.spec_from_file_location("lyricsync_motion", _MODULE_PATH)
m = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(m)


def _sample(t, dx=0.0, dy=0.0, scale=0.0, mag=0.1):
    return {"t": t, "dx": dx, "dy": dy, "scale": scale, "mag": mag}


class TestBucketBySecond:
    def test_averages_samples_within_a_second(self):
        samples = [_sample(0.1, dx=2.0, mag=2.0), _sample(0.6, dx=4.0, mag=4.0)]
        buckets = m.bucket_by_second(samples, duration=1.0)
        assert len(buckets) == 1
        assert buckets[0]["t"] == 0
        assert buckets[0]["dx"] == 3.0
        assert buckets[0]["mag"] == 3.0

    def test_jitter_is_spread_of_dx_dy(self):
        # Same net direction zero, but dx swings ±3 → high jitter.
        samples = [_sample(0.0, dx=3.0), _sample(0.3, dx=-3.0)]
        buckets = m.bucket_by_second(samples, duration=1.0)
        assert buckets[0]["dx"] == 0.0
        assert buckets[0]["jitter"] > 0.0

    def test_missing_seconds_are_omitted(self):
        samples = [_sample(0.0), _sample(5.0)]
        buckets = m.bucket_by_second(samples, duration=6.0)
        assert [b["t"] for b in buckets] == [0, 5]

    def test_empty_samples(self):
        assert m.bucket_by_second([], duration=3.0) == []


def _bucket(dx=0.0, dy=0.0, scale=0.0, mag=0.1, jitter=0.0, t=0):
    return {"t": t, "dx": dx, "dy": dy, "scale": scale, "mag": mag, "jitter": jitter}


class TestClassifyBucket:
    def test_static_when_no_motion(self):
        assert m.classify_bucket(_bucket(mag=0.2)) == "static"

    def test_whip_on_huge_magnitude(self):
        assert m.classify_bucket(_bucket(mag=15.0, dx=12.0)) == "whip"

    def test_zoom_in_on_positive_divergence(self):
        assert m.classify_bucket(_bucket(mag=2.0, scale=0.05)) == "zoom_in"

    def test_zoom_out_on_negative_divergence(self):
        assert m.classify_bucket(_bucket(mag=2.0, scale=-0.05)) == "zoom_out"

    def test_pan_when_horizontal_dominates(self):
        assert m.classify_bucket(_bucket(mag=2.0, dx=3.0, dy=0.2)) == "pan"

    def test_tilt_when_vertical_dominates(self):
        assert m.classify_bucket(_bucket(mag=2.0, dx=0.2, dy=3.0)) == "tilt"

    def test_handheld_when_motion_but_no_net_direction(self):
        assert m.classify_bucket(
            _bucket(mag=2.0, dx=0.1, dy=0.1, jitter=3.0)
        ) == "handheld"

    def test_zoom_takes_priority_over_pan(self):
        # Strong radial divergence + some horizontal drift → still a zoom.
        assert m.classify_bucket(_bucket(mag=2.0, scale=0.05, dx=2.0)) == "zoom_in"


class TestMergeLabelSpans:
    def test_contiguous_same_label_merges(self):
        seconds = m.label_seconds([
            _bucket(t=0, dx=3.0, mag=2.0),
            _bucket(t=1, dx=3.0, mag=2.0),
            _bucket(t=2, mag=0.1),
        ])
        spans = m.merge_label_spans(seconds)
        assert spans == [
            {"start": 0, "end": 2, "label": "pan"},
            {"start": 2, "end": 3, "label": "static"},
        ]

    def test_gap_breaks_a_span(self):
        seconds = m.label_seconds([
            _bucket(t=0, dx=3.0, mag=2.0),
            _bucket(t=2, dx=3.0, mag=2.0),
        ])
        spans = m.merge_label_spans(seconds)
        assert len(spans) == 2
        assert spans[0] == {"start": 0, "end": 1, "label": "pan"}
        assert spans[1] == {"start": 2, "end": 3, "label": "pan"}


class TestSummarize:
    def test_dominant_and_counts(self):
        spans = [
            {"start": 0, "end": 4, "label": "static"},
            {"start": 4, "end": 9, "label": "pan"},
        ]
        summary = m.summarize(spans, scene_cuts=[4.0], duration=9.0)
        assert summary["dominant"] == "pan"
        assert summary["label_seconds"] == {"static": 4, "pan": 5}
        assert summary["n_scene_cuts"] == 1

    def test_empty(self):
        summary = m.summarize([], scene_cuts=[], duration=0.0)
        assert summary["dominant"] is None
        assert summary["label_seconds"] == {}


class TestBuildMotionDoc:
    def test_full_doc_shape(self):
        samples = [
            _sample(0.0, mag=0.1),
            _sample(1.0, dx=3.0, mag=2.0),
            _sample(2.0, dx=3.0, mag=2.0),
        ]
        doc = m.build_motion_doc(
            duration=3.0, fps_sampled=3.0, samples=samples, scene_cuts=[1.0],
        )
        assert doc["version"] == m.MOTION_VERSION
        assert doc["duration"] == 3.0
        assert doc["fps_sampled"] == 3.0
        assert len(doc["seconds"]) == 3
        assert doc["spans"] == [
            {"start": 0, "end": 1, "label": "static"},
            {"start": 1, "end": 3, "label": "pan"},
        ]
        assert doc["scene_cuts"] == [1.0]
        assert doc["summary"]["dominant"] == "pan"

    def test_scene_cuts_default_empty_and_sorted(self):
        doc = m.build_motion_doc(
            duration=1.0, fps_sampled=3.0, samples=[_sample(0.0)],
        )
        assert doc["scene_cuts"] == []


class TestParseSceneCuts:
    def test_parses_pts_times(self):
        stderr = (
            "[Parsed_showinfo_1 @ 0x1] n:0 pts:100 pts_time:4.2 foo\n"
            "[Parsed_showinfo_1 @ 0x1] n:1 pts:225 pts_time:9.0 bar\n"
        )
        assert m.parse_scene_cuts(stderr) == [4.2, 9.0]

    def test_dedupes_adjacent_frames_within_min_gap(self):
        stderr = (
            "pts_time:4.20\n"
            "pts_time:4.25\n"   # same cut, two frames
            "pts_time:9.00\n"
        )
        assert m.parse_scene_cuts(stderr, min_gap=0.5) == [4.2, 9.0]

    def test_no_match_returns_empty(self):
        assert m.parse_scene_cuts("nothing here") == []

    def test_empty_input(self):
        assert m.parse_scene_cuts("") == []
