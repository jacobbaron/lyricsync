"""VAD x transcript alignment experiment harness (offline, in-session).

Goal: find the best way to fuse Silero VAD with WhisperX word timings, and the
parameters to hardcode — NOT to ship knobs. Evaluation has two tiers:

  Tier A (here): automatic metrics over a parameter sweep, scored against TWO
    independent speech references — Silero VAD itself and a separate RMS-energy
    detector computed from the waveform (so the yardstick isn't circular).
  Tier B (you): render boundary/before-after audio for the top survivors so the
    final "sounds right" call is made by ear.

Pure-ish: numpy only. Reuses the tested intervals_from_curve gate from
modal/audio_analysis.py.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import numpy as np

# Reuse the tested hysteresis gate.
_aa_path = Path(__file__).resolve().parents[2] / "modal" / "audio_analysis.py"
_spec = importlib.util.spec_from_file_location("aa", _aa_path)
aa = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(aa)
intervals_from_curve = aa.intervals_from_curve

Interval = tuple[float, float]


# --------------------------------------------------------------------------
# Interval algebra (seconds)
# --------------------------------------------------------------------------

def to_pairs(ivs) -> list[Interval]:
    return [(float(i["start"]), float(i["end"])) if isinstance(i, dict) else (float(i[0]), float(i[1])) for i in ivs]


def total(ivs: list[Interval]) -> float:
    return sum(e - s for s, e in ivs)


def complement(ivs: list[Interval], dur: float) -> list[Interval]:
    out: list[Interval] = []
    cur = 0.0
    for s, e in sorted(ivs):
        if s > cur:
            out.append((cur, s))
        cur = max(cur, e)
    if cur < dur:
        out.append((cur, dur))
    return out


def intersect(a: list[Interval], b: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    i = j = 0
    a, b = sorted(a), sorted(b)
    while i < len(a) and j < len(b):
        lo = max(a[i][0], b[j][0])
        hi = min(a[i][1], b[j][1])
        if hi > lo:
            out.append((lo, hi))
        if a[i][1] < b[j][1]:
            i += 1
        else:
            j += 1
    return out


def merge(ivs: list[Interval]) -> list[Interval]:
    out: list[Interval] = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1] = (out[-1][0], max(out[-1][1], e))
        else:
            out.append((s, e))
    return out


# --------------------------------------------------------------------------
# Independent reference: RMS-energy speech detector (NOT Silero)
# --------------------------------------------------------------------------

def energy_speech(wav: np.ndarray, sr: int, hop: float = 0.01, win: float = 0.025,
                  margin_db: float = 8.0, min_speech: float = 0.10,
                  min_silence: float = 0.10) -> list[Interval]:
    """Speech intervals from short-time RMS energy, thresholded relative to the
    clip's own noise floor. Independent of Silero, so it's a fair cross-check."""
    hop_n = max(1, int(sr * hop))
    win_n = max(hop_n, int(sr * win))
    n = 1 + max(0, (len(wav) - win_n) // hop_n)
    rms = np.empty(n, dtype=np.float32)
    for i in range(n):
        seg = wav[i * hop_n: i * hop_n + win_n]
        rms[i] = np.sqrt(np.mean(seg.astype(np.float64) ** 2) + 1e-12)
    db = 20 * np.log10(rms + 1e-9)
    floor = np.percentile(db, 10)  # noise floor estimate
    thr = floor + margin_db
    pseudo = (db > thr).astype(float).tolist()
    return to_pairs(intervals_from_curve(
        pseudo, hop, threshold=0.5, neg_threshold=0.5,
        min_speech=min_speech, min_silence=min_silence, total=len(wav) / sr,
    ))


# --------------------------------------------------------------------------
# VAD keep-segmentation (the silence-crop strategy family)
# --------------------------------------------------------------------------

def vad_keep(prob, hop, dur, tau=0.5, tau_off=None, min_speech=0.10,
             min_silence=0.20, pad=0.04, protect_over=None,
             collapse_to=None) -> list[Interval]:
    """KEEP intervals = VAD speech (hysteresis tau/tau_off) padded by `pad`,
    joined when the silence between them is < min_silence. Long protected gaps
    are left intact; otherwise removed (or collapsed to `collapse_to`)."""
    sp = to_pairs(intervals_from_curve(
        prob, hop, threshold=tau, neg_threshold=tau_off,
        min_speech=min_speech, min_silence=min_silence, pad=pad, total=dur,
    ))
    if not sp:
        return [(0.0, dur)]
    keep = merge(sp)
    # protect long silences (don't cut deliberate pauses)
    if protect_over is not None:
        gaps = complement(keep, dur)
        protect = [(s, e) for s, e in gaps if e - s >= protect_over]
        keep = merge(keep + protect)
    return keep


def _overlaps_word(s: float, e: float, words, slack: float = 0.15) -> bool:
    for w in words:
        if float(w["end"]) + slack >= s and float(w["start"]) - slack <= e:
            return True
    return False


def fused_keep(prob, hop, words, wav: np.ndarray, sr: int, dur: float,
               tau: float = 0.5, tau_off: float = 0.35, eng_db: float = 12.0,
               pad: float = 0.05, min_silence: float = 0.25) -> list[Interval]:
    """Three-signal silence crop (the chosen strategy).

    A region is KEPT if Silero VAD calls it speech, OR an RMS-energy burst
    coincides with a transcript word (recovers fricative onsets/offsets and
    low/unvoiced speech that VAD misses, e.g. a leading /s/, while ignoring
    wordless room tone). Everything else — where all three say "nothing" — is
    cut. Spans are padded and gaps shorter than `min_silence` are re-closed so
    natural micro-pauses survive.
    """
    vad = to_pairs(intervals_from_curve(
        prob, hop, threshold=tau, neg_threshold=tau_off,
        min_speech=0.10, min_silence=min_silence, pad=0.0, total=dur))
    eng = energy_speech(wav, sr, margin_db=eng_db, min_speech=0.05,
                        min_silence=min_silence)
    eng = [(s, e) for s, e in eng if _overlaps_word(s, e, words)]
    keep = merge([(max(0.0, s - pad), min(dur, e + pad)) for s, e in (vad + eng)])
    closed: list[Interval] = []
    for s, e in keep:
        if closed and s - closed[-1][1] < min_silence:
            closed[-1] = (closed[-1][0], e)
        else:
            closed.append((s, e))
    return closed


# --------------------------------------------------------------------------
# Word boundary reconciliation (Experiment B)
# --------------------------------------------------------------------------

def reconcile_words(words, speech: list[Interval], mode="bias"):
    """Snap edge-word outer boundaries to the VAD speech edges; handle interior
    boundaries per `mode`: 'raw' (keep transcript), 'bias' (shift by measured
    median lead), 'snap_only' (same as raw interior). Returns (refined, stats)."""
    ws = [dict(text=w["text"], start=float(w["start"]), end=float(w["end"])) for w in words]
    onsets = [s for s, _ in speech]
    offsets = [e for _, e in speech]

    def nearest(xs, t):
        return min(xs, key=lambda x: abs(x - t)) if xs else t

    # measure lead bias on edge words (first/last word in each speech interval)
    lead_starts, lead_ends = [], []
    for s, e in speech:
        inside = [w for w in ws if w["start"] < e and w["end"] > s]
        if not inside:
            continue
        lead_starts.append(inside[0]["start"] - s)   # neg => box starts early
        lead_ends.append(inside[-1]["end"] - e)       # neg => box ends early
    delta_start = float(np.median(lead_starts)) if lead_starts else 0.0
    delta_end = float(np.median(lead_ends)) if lead_ends else 0.0
    delta = float(np.median(lead_starts + lead_ends)) if (lead_starts or lead_ends) else 0.0

    refined = []
    for w in ws:
        a, b = w["start"], w["end"]
        # is this word an edge word (its start/end near a speech transition)?
        on = nearest(onsets, a)
        off = nearest(offsets, b)
        ns, ne = a, b
        if abs(on - a) <= 0.20:      # start adjacent to a speech onset -> snap
            ns = on
        elif mode == "bias":
            ns = a - delta
        if abs(off - b) <= 0.20:     # end adjacent to a speech offset -> snap
            ne = off
        elif mode == "bias":
            ne = b - delta
        refined.append(dict(text=w["text"], start=round(ns, 3), end=round(max(ns + 0.01, ne), 3)))

    # keep order monotone
    for i in range(1, len(refined)):
        if refined[i]["start"] < refined[i - 1]["start"]:
            refined[i]["start"] = refined[i - 1]["start"]
        if refined[i]["end"] < refined[i]["start"]:
            refined[i]["end"] = refined[i]["start"] + 0.01

    stats = dict(delta_start=round(delta_start, 3), delta_end=round(delta_end, 3),
                 delta=round(delta, 3), n_edge=len(lead_starts))
    return refined, stats


# --------------------------------------------------------------------------
# Metrics
# --------------------------------------------------------------------------

def score_keep(keep: list[Interval], dur: float, ref_vad: list[Interval],
               ref_energy: list[Interval]) -> dict:
    """How good is a keep-segmentation? speech_cut = voice we wrongly removed
    (vs each independent reference); silence_kept = silence we failed to remove;
    saved = how much we cut overall."""
    removed = complement(keep, dur)
    ref_union = merge(ref_vad + ref_energy)
    sil_ref = complement(ref_union, dur)  # frames neither detector calls speech
    return dict(
        saved_s=round(total(removed), 2),
        speech_cut_vad_ms=round(total(intersect(removed, ref_vad)) * 1000),
        speech_cut_energy_ms=round(total(intersect(removed, ref_energy)) * 1000),
        speech_cut_union_ms=round(total(intersect(removed, ref_union)) * 1000),
        silence_kept_s=round(total(intersect(keep, sil_ref)), 2),
        n_cuts=len(removed) - (1 if removed and removed[0][0] == 0 else 0),
    )
