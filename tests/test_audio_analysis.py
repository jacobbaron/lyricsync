"""Tests for the pure audio-analysis helpers (waveform / VAD shaping).

Loads modal/audio_analysis.py by path (modal/ is not an importable package).
The module is stdlib-only.
"""

import importlib.util
from pathlib import Path

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "audio_analysis.py"
_spec = importlib.util.spec_from_file_location("lyricsync_audio_analysis", _MODULE_PATH)
aa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aa)


class TestDownsample:
    def test_factor_one_is_identity(self):
        assert aa.downsample([0.1, 0.2, 0.3], 1) == [0.1, 0.2, 0.3]

    def test_averages_in_bins(self):
        assert aa.downsample([0.0, 1.0, 0.0, 1.0], 2) == [0.5, 0.5]

    def test_partial_final_bin(self):
        # last bin averages the 1 remaining sample
        assert aa.downsample([0.0, 1.0, 0.6], 2) == [0.5, 0.6]

    def test_empty(self):
        assert aa.downsample([], 4) == []


class TestIntervalsFromCurve:
    def test_empty_curve(self):
        assert aa.intervals_from_curve([], 0.032) == []

    def test_single_speech_run(self):
        # 10 windows of speech at hop 0.1s = 1.0s of speech
        prob = [0.9] * 10
        ivs = aa.intervals_from_curve(prob, 0.1, min_speech=0.1, pad=0.0)
        assert len(ivs) == 1
        assert ivs[0]["start"] == 0.0
        assert ivs[0]["end"] == 1.0

    def test_silence_splits_runs(self):
        # speech, long silence, speech -> two intervals
        prob = [0.9, 0.9, 0.9] + [0.0] * 5 + [0.9, 0.9, 0.9]
        ivs = aa.intervals_from_curve(
            prob, 0.1, min_speech=0.1, min_silence=0.2, pad=0.0,
        )
        assert len(ivs) == 2
        assert ivs[0] == {"start": 0.0, "end": 0.3}
        assert ivs[1]["start"] == 0.8

    def test_brief_dip_does_not_split(self):
        # one sub-threshold window (0.1s) < min_silence (0.3s) stays one run
        prob = [0.9, 0.9, 0.1, 0.9, 0.9]
        ivs = aa.intervals_from_curve(
            prob, 0.1, min_speech=0.1, min_silence=0.3, pad=0.0,
        )
        assert len(ivs) == 1
        assert ivs[0] == {"start": 0.0, "end": 0.5}

    def test_short_run_dropped(self):
        # a 0.1s blip below min_speech 0.25s is discarded
        prob = [0.0, 0.9, 0.0, 0.0, 0.0]
        ivs = aa.intervals_from_curve(
            prob, 0.1, min_speech=0.25, min_silence=0.1, pad=0.0,
        )
        assert ivs == []

    def test_padding_and_merge(self):
        # two runs separated by a hair; padding makes them overlap -> merged
        prob = [0.9, 0.9] + [0.0] * 3 + [0.9, 0.9]
        ivs = aa.intervals_from_curve(
            prob, 0.1, min_speech=0.1, min_silence=0.2, pad=0.25,
        )
        assert len(ivs) == 1

    def test_clamped_to_total(self):
        prob = [0.9] * 5
        ivs = aa.intervals_from_curve(
            prob, 0.1, min_speech=0.1, pad=0.5, total=0.5,
        )
        assert ivs[0]["end"] == 0.5
        assert ivs[0]["start"] == 0.0


class TestWordsInClip:
    def test_filters_by_source_and_shapes(self):
        words = [
            {"text": "hello", "source": "A.mov", "local_start": 1.0,
             "local_end": 1.4, "score": 0.9},
            {"text": "there", "source": "B.mov", "local_start": 2.0,
             "local_end": 2.4},
        ]
        out = aa.words_in_clip(words, "A.mov")
        assert out == [{"text": "hello", "start": 1.0, "end": 1.4, "score": 0.9}]

    def test_sorted_and_skips_untimed(self):
        words = [
            {"text": "b", "source": "A.mov", "local_start": 2.0, "local_end": 2.2},
            {"text": "a", "source": "A.mov", "local_start": 1.0, "local_end": 1.2},
            {"text": "x", "source": "A.mov"},  # no timing -> skipped
        ]
        out = aa.words_in_clip(words, "A.mov")
        assert [w["text"] for w in out] == ["a", "b"]

    def test_falls_back_to_plain_keys(self):
        words = [{"word": "hi", "source": "A.mov", "start": 0.5, "end": 0.9}]
        out = aa.words_in_clip(words, "A.mov")
        assert out == [{"text": "hi", "start": 0.5, "end": 0.9, "score": None}]


class TestBuildAnalysis:
    def test_assembles_document(self):
        doc = aa.build_analysis(
            duration=10.0, audio_key="k.m4a",
            waveform_hop=0.02, peaks=[0.1, 0.2],
            vad_hop=0.032, vad_prob=[0.9, 0.1],
            intervals=[{"start": 0.0, "end": 1.0}],
            words=[{"text": "hi", "start": 0.0, "end": 0.5, "score": None}],
        )
        assert doc["version"] == aa.ANALYSIS_VERSION
        assert doc["duration"] == 10.0
        assert doc["waveform"]["peaks"] == [0.1, 0.2]
        assert doc["vad"]["intervals"] == [{"start": 0.0, "end": 1.0}]
        assert doc["words"][0]["text"] == "hi"

    def test_null_prob_passthrough(self):
        doc = aa.build_analysis(
            10.0, "k.m4a", 0.02, [], 0.032, None, [], [],
        )
        assert doc["vad"]["prob"] is None
