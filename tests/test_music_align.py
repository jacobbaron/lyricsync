"""Tests for modal/music_align.py (chroma-DTW audio matching).

Skipped where librosa/soundfile aren't installed (they live only in the
music-align Modal image, not the default test env). Loads the module by path
for the same reason as test_timeline.py.
"""

import importlib.util
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")
pytest.importorskip("librosa")
sf = pytest.importorskip("soundfile")

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "music_align.py"
_spec = importlib.util.spec_from_file_location("lyricsync_music_align", _MODULE_PATH)
ma = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ma)

SR = 22050


def _melody(note_dur=0.4):
    """A short 8-note melody (pitched content chroma can lock onto)."""
    freqs = [261.6, 329.6, 392.0, 523.3, 392.0, 329.6, 261.6, 392.0]  # C E G C…
    out = []
    for f in freqs:
        t = np.linspace(0, note_dur, int(SR * note_dur), endpoint=False)
        out.append(0.5 * np.sin(2 * np.pi * f * t))
    return np.concatenate(out).astype(np.float32)


def test_align_finds_embedded_melody(tmp_path):
    mel = _melody()
    song = np.concatenate([
        np.zeros(int(SR * 5.0), np.float32),  # 5s lead-in
        mel,
        np.zeros(int(SR * 4.0), np.float32),
    ])
    sp, qp = tmp_path / "song.wav", tmp_path / "query.wav"
    sf.write(sp, song, SR)
    sf.write(qp, mel, SR)

    res = ma.align(str(qp), str(sp))
    assert res["song_start"] == pytest.approx(5.0, abs=0.5)
    assert res["dur_ratio"] == pytest.approx(1.0, abs=0.25)
    assert res["cost"] < 0.1  # identical content → very low cost


def test_wrong_content_scores_worse(tmp_path):
    """The true melody matches the song better (lower cost) than white noise."""
    mel = _melody()
    song = np.concatenate([np.zeros(int(SR * 3.0), np.float32), mel])
    rng = np.random.default_rng(0)
    noise = (rng.standard_normal(len(mel)) * 0.3).astype(np.float32)

    sp, good_q, bad_q = tmp_path / "song.wav", tmp_path / "mel.wav", tmp_path / "noise.wav"
    sf.write(sp, song, SR)
    sf.write(good_q, mel, SR)
    sf.write(bad_q, noise, SR)

    good = ma.align(str(good_q), str(sp))
    bad = ma.align(str(bad_q), str(sp))
    assert good["cost"] < bad["cost"]
