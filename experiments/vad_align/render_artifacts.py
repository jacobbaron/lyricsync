"""Render Tier-B listening artifacts for the recommended config, so the
silence-crop tightness and the onset-snap can be judged by ear.

Outputs (experiments/vad_align/out/):
  <clip>_A_original.m4a     first ~30s of the clip, untouched
  <clip>_B_silencecrop.m4a  same region with VAD silence removed (recommended)
  <clip>_onsetdemo.m4a      edge words trimmed to TRANSCRIPT start vs VAD onset,
                            back to back ("early box" then "tight VAD") so the
                            ~0.4s lead is audible
"""

import json
import subprocess
import wave
from pathlib import Path

import imageio_ffmpeg
import numpy as np

import harness as H

DATA = Path(__file__).parent / "data"
OUT = Path(__file__).parent / "out"
OUT.mkdir(exist_ok=True)
FF = imageio_ffmpeg.get_ffmpeg_exe()
SR = 16000

# Recommended config from the sweep.
TAU, TAU_OFF, MIN_SIL = 0.5, 0.35, 0.25
PAD_ON, PAD_OFF = 0.03, 0.08   # asymmetric: tight onset, protect unvoiced tails


def load(n):
    a = json.load(open(DATA / f"{n}.json"))["analysis"]
    with wave.open(str(DATA / f"{n}.wav"), "rb") as wf:
        wav = np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
    return a, wav


def write_m4a(samples: np.ndarray, path: Path):
    tmp = path.with_suffix(".wav")
    with wave.open(str(tmp), "wb") as wf:
        wf.setnchannels(1); wf.setsampwidth(2); wf.setframerate(SR)
        wf.writeframes(samples.astype(np.int16).tobytes())
    subprocess.run([FF, "-y", "-loglevel", "error", "-i", str(tmp),
                    "-c:a", "aac", "-b:a", "96k", str(path)], check=True)
    tmp.unlink()


def keep_asym(a, dur):
    """Speech intervals with asymmetric padding (tight onset, padded offset)."""
    sp = H.to_pairs(H.intervals_from_curve(
        a["vad"]["prob"], a["vad"]["hop"], threshold=TAU, neg_threshold=TAU_OFF,
        min_speech=0.10, min_silence=MIN_SIL, pad=0.0, total=dur))
    padded = [(max(0.0, s - PAD_ON), min(dur, e + PAD_OFF)) for s, e in sp]
    return H.merge(padded)


def main():
    for n in sorted(p.stem for p in DATA.glob("*.json")):
        a, wav = load(n)
        dur = a["duration"]
        keep = keep_asym(a, dur)

        # A/B over the first ~30s, but never truncate a kept span mid-word:
        # include whole keep spans that START before the cap, and make the
        # "original" cover the exact same time range.
        cap = min(dur, 30.0)
        seg = [(s, e) for s, e in keep if s < cap]
        end = seg[-1][1] if seg else cap
        orig = wav[: int(end * SR)]
        cropped = np.concatenate([wav[int(s * SR): int(e * SR)] for s, e in seg]) if seg else orig
        write_m4a(orig, OUT / f"{n}_A_original.m4a")
        write_m4a(cropped, OUT / f"{n}_B_silencecrop.m4a")
        print(f"{n}: original {end:.1f}s -> cropped {len(cropped)/SR:.1f}s "
              f"({len(seg)} kept spans)")

        # Onset demo: 3 edge words with the largest lead, transcript-start vs VAD-onset.
        onsets = [s for s, _ in keep]
        beeps = (np.sin(2 * np.pi * 880 * np.arange(int(0.12 * SR)) / SR) * 6000).astype(np.int16)
        gap = np.zeros(int(0.4 * SR), dtype=np.int16)
        leads = []
        for w in a["words"]:
            ws = float(w["start"])
            on = min(onsets, key=lambda x: abs(x - ws)) if onsets else ws
            if abs(on - ws) <= 0.20 and on - ws < -0.12:  # box clearly early
                leads.append((ws - on, w["text"], ws, on))
        leads.sort()  # most negative (earliest) first
        parts = []
        for _, text, ws, on in leads[:3]:
            wlen = 0.9
            a_clip = wav[int(ws * SR): int((ws + wlen) * SR)]      # trim at transcript start
            b_clip = wav[int(on * SR): int((on + wlen) * SR)]      # trim at VAD onset
            parts += [a_clip, gap, beeps, gap, b_clip, gap, gap]
            print(f"   onset demo '{text}': transcript {ws:.2f}s vs VAD {on:.2f}s "
                  f"(early by {ws-on:+.2f}s)")
        if parts:
            write_m4a(np.concatenate(parts), OUT / f"{n}_onsetdemo.m4a")


if __name__ == "__main__":
    main()
