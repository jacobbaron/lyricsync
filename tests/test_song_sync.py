"""Tests for modal/song_sync.py (same-take clip↔song sync).

Needs numpy + scipy only. Loads the module by path for the same reason as
test_timeline.py (modal/ is not a package).
"""

import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
signal = pytest.importorskip("scipy.signal")

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "song_sync.py"
_spec = importlib.util.spec_from_file_location("lyricsync_song_sync", _MODULE_PATH)
ss = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ss)

SR = ss.SR


def _song(seconds=60.0, seed=0):
    """A 'mix': irregular note attacks (decaying tones) plus a noise floor."""
    rng = np.random.default_rng(seed)
    n = int(seconds * SR)
    y = 0.01 * rng.standard_normal(n)
    t = 0.0
    while t < seconds - 1.0:
        t += rng.uniform(0.15, 0.6)
        f = rng.choice([196.0, 261.6, 329.6, 392.0, 523.3, 659.3])
        dur = rng.uniform(0.1, 0.5)
        a = int(t * SR)
        k = np.arange(int(dur * SR))
        note = np.sin(2 * np.pi * f * k / SR) * np.exp(-k / (0.15 * SR))
        y[a: a + len(k)] += 0.4 * note[: max(0, n - a)]
    return y.astype(np.float64)


def _phone(y, seed=1):
    """Degrade like a phone mic in a room: gain, band-limit, echo, noise."""
    rng = np.random.default_rng(seed)
    sos = signal.butter(4, [200, 5000], btype="bandpass", fs=SR, output="sos")
    out = 0.6 * signal.sosfilt(sos, y)
    d = int(0.023 * SR)
    out[d:] += 0.3 * out[:-d]  # a room reflection
    return out + 0.02 * rng.standard_normal(len(out))


def test_finds_offset_to_the_millisecond():
    song = _song()
    start = 7.3456
    clip = _phone(song[int(start * SR): int(start * SR) + int(40 * SR)])
    res = ss.sync(clip, song)
    assert res["trusted"]
    assert res["offset"] == pytest.approx(start, abs=0.002)
    assert res["drift_ms"] < 10
    assert len(res["window_offsets"]) == 5


def test_negative_offset_when_camera_rolls_before_the_master():
    """The clip starts with 3 s of room noise before the song begins."""
    song = _song(40.0, seed=3)
    rng = np.random.default_rng(9)
    lead = 0.02 * rng.standard_normal(int(3.0 * SR))
    clip = _phone(np.concatenate([lead, song[: int(30 * SR)]]))
    res = ss.sync(clip, song)
    assert res["trusted"]
    assert res["offset"] == pytest.approx(-3.0, abs=0.002)


def test_different_take_shows_drift():
    """A take played 0.3% slower can't hold one offset across the clip."""
    song = _song(70.0, seed=5)
    seg = song[int(5 * SR): int(5 * SR) + int(60 * SR)]
    slower = signal.resample(seg, int(len(seg) * 1.003))
    res = ss.sync(_phone(slower), song)
    assert res["drift_ms"] > ss.DRIFT_WARN_MS


def test_unrelated_audio_is_not_trusted():
    song = _song(60.0, seed=7)
    other = _song(30.0, seed=8)
    res = ss.sync(_phone(other), song)
    assert not res["trusted"]


def test_short_clip_skips_drift_windows():
    song = _song(30.0, seed=11)
    clip = _phone(song[int(12 * SR): int(18 * SR)])
    res = ss.sync(clip, song)
    assert res["offset"] == pytest.approx(12.0, abs=0.002)
    assert res["window_offsets"] == []
    assert res["drift_ms"] == 0.0
