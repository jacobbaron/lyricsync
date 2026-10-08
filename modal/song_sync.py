"""Fast clip↔song sync for the same-take case — pure numpy/scipy.

When the uploaded song master is a clean mix of the *same performance* the
cameras filmed (a multi-angle shoot recorded to a desk/DAW), the footage audio
is the master plus room colour, phone-mic EQ and noise. A full chroma-DTW search
(music_align.py) is overkill there: a cross-correlation of onset envelopes finds
the offset in one FFT, and a correlation of sample-rate attack curves around
that offset pins it to about a millisecond. That is what worked by hand on "the
one who runs" (wide = piano + 3.212s, held ±10ms across a 4.5-minute take).

Pipeline (all on mono float arrays at SR):
    onset_envelope  → spectral-flux novelty curve (one value per HOP)
    coarse_offset   → envelope xcorr over every plausible lag + peak ratio
    refine_offset   → attack-curve xcorr in a small band around the coarse lag
    sync            → the above + per-window re-measurement (drift check)

`offset` everywhere is song_time - clip_time: song time = clip time + offset.
It can be negative (the camera was rolling before the master starts).

The peak ratio (best correlation / best rival outside a guard band) is the
confidence: a same-take match is a lone spike, a different take or unrelated
audio is a field of similar bumps. Drift is the spread of offsets re-measured
in windows across the clip: a same take holds within a few ms; a different
take wanders, which means lip-sync will wander too.

No librosa/Modal deps, so it imports cleanly in tests (numpy + scipy only).
"""

from __future__ import annotations

import numpy as np
from scipy import signal

SR = 22050
HOP = 512
N_FFT = 2048

# Below this envelope peak ratio the match isn't trusted and the caller should
# fall back to chroma-DTW (a different take / arrangement).
MIN_PEAK_RATIO = 1.5
# Offsets re-measured across the clip that spread wider than this mean the
# footage is not the same take as the master (lip-sync would wander).
DRIFT_WARN_MS = 40.0

# Rival peaks closer than this to the best one are the same peak's shoulders.
_GUARD_S = 0.5
# A lag must overlap at least this fraction of the shorter signal to count, so
# a two-second sliver at either edge can't win on a lucky transient.
_MIN_OVERLAP_FRAC = 0.5
# Refinement searches ± this around the coarse lag (a couple of hops).
_REFINE_S = 0.06
# Cap the clip chunk used for refinement (cost is FFT of chunk + band).
_REFINE_CHUNK_S = 20.0


def _frames_to_s(n: float) -> float:
    return n * HOP / SR


def onset_envelope(y: np.ndarray) -> np.ndarray:
    """Spectral-flux novelty: summed positive change in log-magnitude per hop.

    Keys on *when* things happen (note attacks, consonants, drum hits), not on
    timbre, so a phone mic and a desk mix of the same take produce matching
    curves. Zero-mean, unit-variance so correlation scores are comparable.
    """
    y = np.asarray(y, dtype=np.float32)
    if len(y) < N_FFT:
        y = np.pad(y, (0, N_FFT - len(y)))
    # Frame + FFT in blocks so an hour-long clip doesn't need its whole
    # spectrogram in memory at once (only the per-frame flux is kept).
    win = signal.get_window("hann", N_FFT).astype(np.float32)
    win /= win.sum()
    n_frames = 1 + (len(y) - N_FFT) // HOP
    flux = np.empty(max(0, n_frames - 1), dtype=np.float64)
    prev = None
    block = 4096  # frames per block (~95 s)
    for f0 in range(0, n_frames, block):
        f1 = min(n_frames, f0 + block)
        seg = y[f0 * HOP: (f1 - 1) * HOP + N_FFT]
        frames = np.lib.stride_tricks.sliding_window_view(seg, N_FFT)[::HOP]
        mag = np.log1p(100.0 * np.abs(np.fft.rfft(frames * win, axis=1)))
        if prev is not None:
            mag = np.vstack([prev, mag])
        d = np.maximum(np.diff(mag, axis=0), 0.0).sum(axis=1)
        start = f0 - 1 if prev is not None else 0
        flux[start: start + len(d)] = d
        prev = mag[-1:]
    flux = np.concatenate([[0.0], flux])
    # Remove the slow loudness trend so a crescendo doesn't dominate the match.
    k = max(1, int(round(1.0 * SR / HOP)))
    trend = np.convolve(flux, np.ones(k) / k, mode="same")
    env = flux - trend
    sd = env.std()
    return (env - env.mean()) / sd if sd > 0 else env * 0.0


def _peak_ratio(xc: np.ndarray, best: int, guard: int) -> float:
    """Best correlation over the best rival outside ±guard of it."""
    peak = float(xc[best])
    mask = np.ones(len(xc), dtype=bool)
    mask[max(0, best - guard): best + guard + 1] = False
    if not mask.any() or peak <= 0:
        return 0.0
    rival = float(xc[mask].max())
    return peak / rival if rival > 1e-9 else float("inf")


def coarse_offset(
    q_env: np.ndarray,
    s_env: np.ndarray,
    around: float | None = None,
    search_s: float | None = None,
) -> tuple[float, float]:
    """Envelope cross-correlation → (offset seconds, peak ratio).

    `around`/`search_s` restrict the lag search to around ± search_s (used by
    the per-window drift check); by default every lag with enough overlap is
    considered.
    """
    nq, ns = len(q_env), len(s_env)
    xc = signal.correlate(s_env, q_env, mode="full", method="fft")
    lags = np.arange(-(nq - 1), ns)  # s index = q index + lag
    overlap = np.minimum(ns, lags + nq) - np.maximum(0, lags)
    valid = overlap >= max(1, int(_MIN_OVERLAP_FRAC * min(nq, ns)))
    if around is not None and search_s is not None:
        c = around * SR / HOP
        w = search_s * SR / HOP
        valid &= (lags >= c - w) & (lags <= c + w)
    if not valid.any():
        return 0.0, 0.0
    xc = np.where(valid, xc, -np.inf)
    best = int(np.argmax(xc))
    guard = max(1, int(_GUARD_S * SR / HOP))
    ratio = _peak_ratio(np.where(valid, xc, -np.inf), best, guard)
    return _frames_to_s(int(lags[best])), ratio


def _attack_curve(y: np.ndarray) -> np.ndarray:
    """Sample-rate amplitude envelope, differentiated into an attack curve.

    Correlating raw waveforms of tonal music locks onto the wrong cycle of a
    sustained note about as often as the right one (every period is a near-
    identical peak). The amplitude envelope has no carrier, so its correlation
    peak is unique, and its rising edges still place attacks to ~1 ms.
    """
    y = np.asarray(y, dtype=np.float64)
    # Drop rumble/handling noise below ~120 Hz first.
    sos = signal.butter(2, 120.0, btype="highpass", fs=SR, output="sos")
    env = np.abs(signal.hilbert(signal.sosfiltfilt(sos, y)))
    # ~1 ms smoothing, then keep only rises (attacks), zero-mean.
    k = max(1, int(0.001 * SR))
    env = np.convolve(env, np.ones(k) / k, mode="same")
    att = np.maximum(np.diff(env, prepend=env[0]), 0.0)
    return att - att.mean()


def refine_offset(
    q: np.ndarray,
    s: np.ndarray,
    offset: float,
    q_start: float | None = None,
    chunk_s: float = _REFINE_CHUNK_S,
    search_s: float = _REFINE_S,
) -> float:
    """Sample-accurate offset: attack-curve xcorr within ±search_s of `offset`.

    Uses a chunk of the clip (default: the middle of the overlap) and the
    matching stretch of song widened by the search band. Returns `offset`
    unchanged when the chunk falls outside either signal.
    """
    nq, ns = len(q), len(s)
    off = int(round(offset * SR))
    lo = max(0, -off)  # first clip sample that lands inside the song
    hi = min(nq, ns - off)
    if hi - lo < int(0.5 * SR):
        return offset
    n = min(hi - lo, int(chunk_s * SR))
    a = (
        int(q_start * SR) if q_start is not None
        else lo + (hi - lo - n) // 2
    )
    a = int(np.clip(a, lo, hi - n))
    r = int(search_s * SR)
    s0 = a + off - r
    s1 = a + off + n + r
    if s0 < 0 or s1 > ns:
        # Clamp the band at the song edges; the search just gets narrower.
        s0, s1 = max(0, s0), min(ns, s1)
        if s1 - s0 <= n:
            return offset
    qc = _attack_curve(q[a: a + n])
    sc = _attack_curve(s[s0:s1])
    xc = signal.correlate(sc, qc, mode="valid", method="fft")
    k = int(np.argmax(xc))
    # Parabolic interpolation for a sub-sample peak.
    frac = 0.0
    if 0 < k < len(xc) - 1:
        y0, y1, y2 = xc[k - 1], xc[k], xc[k + 1]
        den = y0 - 2 * y1 + y2
        if den != 0:
            frac = 0.5 * (y0 - y2) / den
    return (s0 + k + frac - a) / SR


def sync(
    clip: np.ndarray,
    song: np.ndarray,
    n_windows: int = 5,
    window_s: float = 10.0,
) -> dict:
    """Locate `clip` inside `song` (same-take fast path).

    Returns:
      offset        : song_time - clip_time, seconds (sample-refined)
      confidence    : envelope peak ratio (>= MIN_PEAK_RATIO is trusted)
      trusted       : confidence >= MIN_PEAK_RATIO
      drift_ms      : spread of the offset re-measured in windows across the
                      clip; > DRIFT_WARN_MS means a different take
      window_offsets: the per-window offsets (seconds)
      overlap_s     : how much of the clip lands inside the song
    """
    q_env = onset_envelope(clip)
    s_env = onset_envelope(song)
    coarse, ratio = coarse_offset(q_env, s_env)
    offset = refine_offset(clip, song, coarse)
    # The refinement must agree with the coarse lag to within its search band;
    # otherwise trust the coarse estimate.
    if abs(offset - coarse) > _REFINE_S + 1e-6:
        offset = coarse

    lo = max(0.0, -offset)
    hi = min(len(clip) / SR, len(song) / SR - offset)
    overlap = max(0.0, hi - lo)

    window_offsets: list[float] = []
    if overlap >= 2 * window_s and n_windows > 1:
        starts = np.linspace(lo, hi - window_s, n_windows)
        for t0 in starts:
            a, b = int(t0 * SR), int((t0 + window_s) * SR)
            w_env = onset_envelope(clip[a:b])
            # Re-locate the window on its own (±1 s around the global lag), so
            # a slow tempo difference shows up as a moving offset.
            w_coarse, _ = coarse_offset(
                w_env, s_env, around=t0 + offset, search_s=1.0
            )
            w_coarse -= t0  # window lag → clip-relative offset
            w_off = refine_offset(clip, song, w_coarse, q_start=t0,
                                  chunk_s=window_s)
            if abs(w_off - w_coarse) > _REFINE_S + 1e-6:
                w_off = w_coarse
            window_offsets.append(round(float(w_off), 4))

    drift_ms = (
        (max(window_offsets) - min(window_offsets)) * 1000.0
        if len(window_offsets) >= 2 else 0.0
    )
    return {
        "offset": round(float(offset), 4),
        "confidence": round(float(min(ratio, 1e6)), 3),
        "trusted": bool(ratio >= MIN_PEAK_RATIO),
        "drift_ms": round(float(drift_ms), 1),
        "window_offsets": window_offsets,
        "overlap_s": round(float(overlap), 3),
    }
