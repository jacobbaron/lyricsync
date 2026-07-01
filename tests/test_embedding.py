"""Tests for the pure embedding helpers (PERCEPTION T4).

Loads modal/embedding.py by path (modal/ is not an importable package). The
module is stdlib-only.
"""

import importlib.util
import math
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "embedding.py"
_spec = importlib.util.spec_from_file_location("lyricsync_embedding", _MODULE_PATH)
e = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(e)


class TestFrameTime:
    def test_default_fps_is_one_per_second(self):
        assert e.frame_time(0) == 0.0
        assert e.frame_time(5) == 5.0

    def test_respects_fps(self):
        assert e.frame_time(3, fps=2.0) == 1.5


class TestL2Normalize:
    def test_unit_norm(self):
        out = e.l2_normalize([3.0, 4.0])
        assert out == pytest.approx([0.6, 0.8])
        assert math.isclose(math.sqrt(sum(x * x for x in out)), 1.0)

    def test_zero_vector_unchanged(self):
        assert e.l2_normalize([0.0, 0.0, 0.0]) == [0.0, 0.0, 0.0]


class TestMeanPool:
    def test_averages_then_normalizes(self):
        # Mean of the two axis vectors is (0.5, 0.5); normalized → (√½, √½).
        out = e.mean_pool([[1.0, 0.0], [0.0, 1.0]])
        assert out == pytest.approx([1 / math.sqrt(2), 1 / math.sqrt(2)])

    def test_single_vector_is_just_normalized(self):
        assert e.mean_pool([[3.0, 4.0]]) == pytest.approx([0.6, 0.8])

    def test_empty_raises(self):
        with pytest.raises(ValueError):
            e.mean_pool([])

    def test_ragged_raises(self):
        with pytest.raises(ValueError):
            e.mean_pool([[1.0, 2.0], [1.0]])


class TestToPgvector:
    def test_formats_bracketed_csv(self):
        assert e.to_pgvector([0.5, -1.0, 2.0]) == "[0.5,-1.0,2.0]"

    def test_roundtrips_through_float(self):
        s = e.to_pgvector([1, 2, 3])  # ints coerced to float
        assert s.startswith("[") and s.endswith("]")
        assert [float(x) for x in s[1:-1].split(",")] == [1.0, 2.0, 3.0]


def test_model_constants():
    assert e.EMBED_MODEL == "clip-ViT-B-32"
    assert e.EMBED_DIM == 512
