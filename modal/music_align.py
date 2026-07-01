"""Chroma-DTW audio matching for the music-sync feature.

Locate a vocal-only footage take inside a finished full-band mix and return the
song time it corresponds to. Robust to the two big differences between the
sources — the footage is a phone/room mic capturing vocals only, the song is a
good mic + full band — because chroma keys on pitch class (not timbre) and
subsequence-DTW absorbs tempo differences.

Validated on the "One Car Ahead" session: verse/pre-chorus/chorus anchors landed
in the song's real structure, two takes of the same line matched to within
0.03s, and a talking control scored far worse than any sung phrase.

librosa/numpy are imported at module load, so only mount this into an image that
installs them (the music-align worker) — never import it at app.py module scope.
"""

from __future__ import annotations

import numpy as np
import librosa

SR = 22050
HOP = 512

# Diagonal-biased DTW steps: an additive penalty on the horizontal/vertical
# moves keeps the path near-diagonal (tempo ~constant), which stops the match
# from collapsing a long query onto a couple of song frames.
_STEP = np.array([[1, 1], [1, 0], [0, 1]])
_WADD = np.array([0.0, 0.4, 0.4])


def _chroma(y: np.ndarray, sr: int = SR) -> np.ndarray:
    """L2-normalized CQT chroma of the harmonic component (drops percussive
    transients so drums in the mix don't swamp the pitch content).

    A tiny floor is added before normalizing so silent regions (a count-in or
    trailing silence common in finished mixes) become a uniform vector rather
    than an all-zero one — otherwise cosine-DTW divides by zero and returns NaN.
    """
    yh = librosa.effects.harmonic(y, margin=3.0)
    c = librosa.feature.chroma_cqt(y=yh, sr=sr, hop_length=HOP)
    c = c + 1e-8
    return librosa.util.normalize(c, norm=2, axis=0)


def _load(path: str, offset: float = 0.0, duration: float | None = None) -> np.ndarray:
    y, _ = librosa.load(path, sr=SR, offset=offset, duration=duration)
    return y


def align(
    query_path: str,
    song_path: str,
    query_offset: float = 0.0,
    query_duration: float | None = None,
) -> dict:
    """Locate `query` (a footage take) inside `song`.

    Returns:
      song_start / song_end : seconds in the song the take maps to
      cost                  : per-frame cosine distance (lower = better; sung
                              phrases ~0.20-0.26, non-singing ~0.34+)
      dur_ratio             : matched-span / query length (~1.0 when healthy;
                              far from 1 signals a poor / collapsed match)
      query_len             : query length in seconds
    """
    q = _chroma(_load(query_path, offset=query_offset, duration=query_duration))
    song = _chroma(_load(song_path))

    d, wp = librosa.sequence.dtw(
        X=q, Y=song, subseq=True, metric="cosine",
        step_sizes_sigma=_STEP, weights_add=_WADD,
    )
    end_col = int(np.argmin(d[-1, :]))
    cost = float(d[-1, end_col] / q.shape[1])

    path = wp[::-1]  # dtw returns end→start; flip to start→end

    def f2s(f: int) -> float:
        return f * HOP / SR

    song_start = f2s(int(path[0, 1]))
    song_end = f2s(int(path[-1, 1]))
    qlen = f2s(q.shape[1])
    return {
        "song_start": round(song_start, 3),
        "song_end": round(song_end, 3),
        "cost": round(cost, 4),
        "dur_ratio": round((song_end - song_start) / qlen, 3) if qlen else 0.0,
        "query_len": round(qlen, 3),
    }
