"""Tests for the pure object-detection shaping helpers (PERCEPTION T5).

Loads modal/detection.py by path (modal/ is not an importable package). The
module is stdlib-only.
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "detection.py"
_spec = importlib.util.spec_from_file_location("lyricsync_detection", _MODULE_PATH)
d = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(d)


def _frame(t, *dets):
    return {"t": t, "detections": [
        {"class": c, "conf": conf, "box": box} for (c, conf, box) in dets
    ]}


class TestIou:
    def test_identical_boxes(self):
        assert d.iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0

    def test_disjoint_boxes(self):
        assert d.iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0

    def test_half_overlap(self):
        # Two 10x10 boxes overlapping in a 5x10 strip: inter=50, union=150.
        assert d.iou([0, 0, 10, 10], [5, 0, 15, 10]) == pytest.approx(50 / 150)


class TestTrackDetections:
    def test_stable_box_makes_one_tracklet(self):
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20])),
            _frame(0.5, ("person", 0.8, [1, 0, 11, 20])),
            _frame(1.0, ("person", 0.85, [0, 1, 10, 21])),
        ]
        tracks = d.track_detections(frames)
        assert len(tracks) == 1
        assert tracks[0]["class"] == "person"
        assert len(tracks[0]["boxes"]) == 3
        assert tracks[0]["start"] == 0.0
        assert tracks[0]["end"] == 1.0

    def test_two_classes_split(self):
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20]), ("chair", 0.7, [50, 50, 70, 80])),
            _frame(0.5, ("person", 0.9, [0, 0, 10, 20]), ("chair", 0.7, [50, 50, 70, 80])),
        ]
        tracks = d.track_detections(frames)
        assert sorted(t["class"] for t in tracks) == ["chair", "person"]

    def test_jump_beyond_iou_starts_new_tracklet(self):
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20])),
            _frame(0.5, ("person", 0.9, [100, 100, 110, 120])),  # no overlap
        ]
        tracks = d.track_detections(frames)
        assert len(tracks) == 2

    def test_gap_within_tolerance_keeps_one_track(self):
        # Missing in the middle frame, reappears in place → still one tracklet
        # (default max_gap_frames=2).
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20])),
            _frame(0.5),  # miss
            _frame(1.0, ("person", 0.9, [0, 0, 10, 20])),
        ]
        tracks = d.track_detections(frames)
        assert len(tracks) == 1
        assert len(tracks[0]["boxes"]) == 2

    def test_gap_beyond_tolerance_splits(self):
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20])),
            _frame(0.5), _frame(1.0), _frame(1.5),  # 3 misses > max_gap 2
            _frame(2.0, ("person", 0.9, [0, 0, 10, 20])),
        ]
        tracks = d.track_detections(frames)
        assert len(tracks) == 2


class TestBuildInventory:
    def test_counts_screen_time_and_boxes(self):
        frames = [
            _frame(0.0, ("person", 0.8, [0, 0, 10, 10])),
            _frame(0.5, ("person", 1.0, [0, 0, 12, 12])),  # IoU 0.69 → same track
            _frame(1.0, ("chair", 0.6, [5, 5, 15, 15])),
        ]
        tracks = d.track_detections(frames)
        inv = d.build_inventory(frames, tracks, fps_sampled=2.0)
        # person present in 2 of the frames → 2/2fps = 1.0s
        assert inv["person"]["screen_time"] == 1.0
        assert inv["person"]["count"] == 1
        assert inv["person"]["mean_conf"] == 0.9
        assert inv["person"]["mean_box"] == [0.0, 0.0, 11.0, 11.0]
        assert inv["chair"]["screen_time"] == 0.5

    def test_distinct_instances_counted(self):
        # Two people far apart in the same frame → two tracklets, count 2.
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20]), ("person", 0.9, [100, 0, 110, 20])),
            _frame(0.5, ("person", 0.9, [0, 0, 10, 20]), ("person", 0.9, [100, 0, 110, 20])),
        ]
        tracks = d.track_detections(frames)
        inv = d.build_inventory(frames, tracks, fps_sampled=2.0)
        assert inv["person"]["count"] == 2
        assert inv["person"]["frames_present"] == 2


class TestBuildResultAndDoc:
    def test_result_is_compact_and_ranked(self):
        frames = [
            _frame(0.0, ("person", 0.9, [0, 0, 10, 20])),
            _frame(0.5, ("person", 0.9, [0, 0, 10, 20]), ("chair", 0.7, [50, 50, 60, 70])),
        ]
        tracks = d.track_detections(frames)
        res = d.build_detection_result(frames, tracks, fps_sampled=2.0, model="yolov8n")
        assert res["model"] == "yolov8n"
        # closed mode is the default and no query is echoed
        assert res["mode"] == "closed"
        assert "query" not in res
        # person has more screen time than chair → ranked first
        assert res["summary"]["classes"][0] == "person"
        assert res["summary"]["n_frames"] == 2
        assert "person" in res["inventory"]
        # compact: no per-frame boxes in the result
        assert "frames" not in res

    def test_doc_keeps_frames_and_summarized_tracklets(self):
        frames = [_frame(0.0, ("person", 0.9, [0, 0, 10, 20]))]
        doc = d.build_detection_doc(frames, duration=1.0, fps_sampled=2.0, model="yolov8n")
        assert doc["version"] == d.DETECTION_VERSION
        assert doc["mode"] == "closed"
        assert doc["n_frames"] == 1
        assert doc["frames"] == frames
        tr = doc["tracklets"][0]
        assert tr["class"] == "person" and tr["n_frames"] == 1
        assert "mean_box" in tr and "boxes" not in tr  # summarized

    def test_open_mode_echoes_mode_and_query(self):
        # Open-vocab shaping is identical; only mode/query metadata differ, and
        # a queried label that matched nothing is absent from inventory.
        frames = [_frame(0.0, ("tape measure", 0.4, [0, 0, 10, 20]))]
        tracks = d.track_detections(frames)
        query = ["tape measure", "flatbed cart"]
        res = d.build_detection_result(
            frames, tracks, fps_sampled=2.0, model="owlv2-base-patch16-ensemble",
            mode="open", query=query,
        )
        assert res["mode"] == "open"
        assert res["query"] == query
        assert "tape measure" in res["inventory"]
        assert "flatbed cart" not in res["inventory"]  # queried, never seen
        doc = d.build_detection_doc(
            frames, duration=1.0, fps_sampled=2.0, model="owlv2-base-patch16-ensemble",
            tracklets=tracks, mode="open", query=query,
        )
        assert doc["mode"] == "open" and doc["query"] == query


class TestDeriveOpenLabels:
    def test_seeds_from_description_then_glossary(self):
        labels = d.derive_open_labels("A workshop bench with a jigsaw and MDF board")
        # description nouns come first, lowercased + stopwords dropped
        assert labels[0] == "workshop"
        assert "bench" in labels and "jigsaw" in labels
        assert "with" not in labels and "and" not in labels  # stopwords
        # glossary is appended as a fallback seed
        assert "tape measure" in labels

    def test_dedupes_and_respects_cap(self):
        labels = d.derive_open_labels("drill drill DRILL", max_labels=5)
        assert labels.count("drill") == 1
        assert len(labels) <= 5

    def test_extra_labels_included_before_glossary(self):
        labels = d.derive_open_labels(None, extra=["Rockwool insulation"])
        assert "rockwool insulation" in labels

    def test_empty_description_falls_back_to_glossary(self):
        labels = d.derive_open_labels(None)
        assert labels == [g.lower() for g in d.DIY_GLOSSARY][: len(labels)]
        assert "insulation" in labels
