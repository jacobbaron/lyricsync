"""Run the VAD x transcript experiments over the fetched clips and print a
report. Tier-A (automatic) only; audio artifacts for Tier-B are rendered by
render_artifacts.py for the winning configs.
"""

import json
import wave
from pathlib import Path

import numpy as np

import harness as H

DATA = Path(__file__).parent / "data"


def load(name):
    d = json.load(open(DATA / f"{name}.json"))
    a = d["analysis"]
    with wave.open(str(DATA / f"{name}.wav"), "rb") as wf:
        sr = wf.getframerate()
        wav = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16).astype(np.float32) / 32768.0
    return a, wav, sr


def main():
    names = sorted(p.stem for p in DATA.glob("*.json"))
    clips = {n: load(n) for n in names}

    # Build references per clip.
    refs = {}
    print("=" * 78)
    print("REFERENCE AGREEMENT  (do the two independent detectors agree on speech?)")
    print("-" * 78)
    print(f"{'clip':14} {'dur':>6} {'VAD_sp':>7} {'Eng_sp':>7} {'overlap':>8} {'IoU':>6}")
    for n in names:
        a, wav, sr = clips[n]
        dur = a["duration"]
        ref_vad = H.vad_keep(a["vad"]["prob"], a["vad"]["hop"], dur,
                             tau=0.5, min_speech=0.1, min_silence=0.1, pad=0.0)
        ref_eng = H.energy_speech(wav, sr, margin_db=14.0)
        inter = H.total(H.intersect(ref_vad, ref_eng))
        union = H.total(H.merge(ref_vad + ref_eng))
        refs[n] = (ref_vad, ref_eng)
        print(f"{n:14} {dur:6.1f} {H.total(ref_vad):7.1f} {H.total(ref_eng):7.1f} "
              f"{inter:8.1f} {inter/union if union else 0:6.2f}")

    # ---- Experiment A: silence-crop parameter sweep --------------------------
    print("\n" + "=" * 78)
    print("EXPERIMENT A — silence cropping: sweep tau / min_silence / pad")
    print("  speech_cut_* = ms of VOICE wrongly removed (lower=better; ~0 is the")
    print("  hard constraint). saved = silence removed. Aggregated over clips.")
    print("-" * 78)
    grid = []
    for tau in (0.4, 0.5, 0.6, 0.7, 0.8):
        for min_sil in (0.15, 0.25, 0.40):
            for pad in (0.0, 0.04, 0.08):
                grid.append((tau, min_sil, pad))

    rows = []
    for tau, min_sil, pad in grid:
        agg = dict(saved=0.0, cut_vad=0, cut_eng=0, cut_union=0, sil_kept=0.0, cuts=0)
        for n in names:
            a, wav, sr = clips[n]
            dur = a["duration"]
            keep = H.vad_keep(a["vad"]["prob"], a["vad"]["hop"], dur, tau=tau,
                              tau_off=max(0.0, tau - 0.15), min_speech=0.1,
                              min_silence=min_sil, pad=pad)
            s = H.score_keep(keep, dur, *refs[n])
            agg["saved"] += s["saved_s"]
            agg["cut_vad"] += s["speech_cut_vad_ms"]
            agg["cut_eng"] += s["speech_cut_energy_ms"]
            agg["cut_union"] += s["speech_cut_union_ms"]
            agg["sil_kept"] += s["silence_kept_s"]
            agg["cuts"] += s["n_cuts"]
        rows.append(((tau, min_sil, pad), agg))

    print(f"{'tau':>4} {'minSil':>6} {'pad':>5} | {'saved_s':>7} {'cut_vad':>7} "
          f"{'cut_eng':>7} {'cut_un':>7} {'silKept':>7} {'cuts':>5}")
    for (tau, ms, pad), agg in rows:
        print(f"{tau:4.1f} {ms:6.2f} {pad:5.2f} | {agg['saved']:7.1f} "
              f"{agg['cut_vad']:7.0f} {agg['cut_eng']:7.0f} {agg['cut_union']:7.0f} "
              f"{agg['sil_kept']:7.1f} {agg['cuts']:5.0f}")

    # Recommend: most saved subject to a Silero-speech-cut budget (the trustworthy
    # gate; energy over-counts and is recording-dependent, so it's secondary).
    print("\nPareto picks (max saved with speech_cut_VAD under a budget):")
    for budget in (0, 100, 300):
        ok = [r for r in rows if r[1]["cut_vad"] <= budget]
        if ok:
            best = max(ok, key=lambda r: r[1]["saved"])
            (tau, ms, pad), agg = best
            print(f"  <= {budget:3d}ms Silero-speech cut -> tau={tau} min_sil={ms} pad={pad}"
                  f"  | saved={agg['saved']:.1f}s cut_vad={agg['cut_vad']:.0f}ms "
                  f"cut_eng={agg['cut_eng']:.0f}ms silKept={agg['sil_kept']:.1f}s cuts={agg['cuts']:.0f}")

    # ---- Experiment B: word-boundary lead bias -------------------------------
    print("\n" + "=" * 78)
    print("EXPERIMENT B — word-boundary lead bias vs VAD edges")
    print("  delta_start/end = transcript_edge - VAD_edge (negative => box is")
    print("  EARLY, the skew you spotted). delta = global median.")
    print("-" * 78)
    print(f"{'clip':14} {'n_edge':>6} {'d_start':>8} {'d_end':>8} {'delta':>7}")
    all_starts, all_ends = [], []
    for n in names:
        a, wav, sr = clips[n]
        ref_vad = refs[n][0]
        _, st = H.reconcile_words(a["words"], ref_vad, mode="bias")
        print(f"{n:14} {st['n_edge']:6d} {st['delta_start']:8.3f} "
              f"{st['delta_end']:8.3f} {st['delta']:7.3f}")


if __name__ == "__main__":
    main()
