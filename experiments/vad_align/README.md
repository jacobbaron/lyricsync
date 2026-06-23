# VAD × transcript alignment experiments

Offline harness to decide how to fuse Silero VAD with WhisperX word timings —
and the constants to hardcode in production. Not shipped as user/API knobs.

## Run

```bash
# fetch + decode test clips (needs LYRICSYNC_API_KEY / _BASE_URL)
python3 fetch is in run.py's data dir; see commands in the PR description
python3 run.py                 # Tier-A automatic sweep + report
python3 render_artifacts.py    # Tier-B listening artifacts (out/*.m4a)
```

- `harness.py` — interval algebra, the independent RMS-energy reference,
  the VAD keep-segmentation, word reconciliation, and metrics. Reuses the
  tested `intervals_from_curve` gate from `modal/audio_analysis.py`.
- `run.py` — reference-agreement check, silence-crop parameter sweep,
  word-boundary lead-bias analysis.
- `render_artifacts.py` — before/after silence-crop + onset-snap demos.

## Evaluation design

No hand-labeled boundaries exist and audio can't be auto-"heard", so:

- **Tier A (automatic):** score against two *independent* speech references —
  Silero VAD and a separate RMS-energy detector — over a parameter grid.
  `speech_cut_vad` (voice wrongly removed, vs Silero) is the trustworthy
  "don't cut speech" gate; the energy detector is recording-dependent and
  used only as a soft cross-check.
- **Tier B (human):** render before/after + boundary clips and judge by ear.

## Findings (test set: IMG_2625 125s, IMG_2627 46s)

- **Silence crop:** τ_on=0.5 / τ_off=0.35 removes ~88s of dead air with
  **0 ms** of Silero-confirmed speech cut. τ≥0.6 starts clipping speech for
  negligible extra savings → **τ=0.5 is the operating point.**
- **Word onsets** are systematically **early** (median −0.35s on 2625,
  −0.73s on 2627), worst on the first word after a silence → **snap onset to
  the VAD edge.**
- **Word offsets** sit *past* the VAD edge (d_end > 0): Silero trims unvoiced
  consonant tails (s/f/t), so **protect the offset** with a tail pad
  (~80 ms), don't snap it.
- A single global bias-shift is **disproven** by the start/end asymmetry.

Recommended constants (pending Tier-B listening confirmation):
`τ_on=0.5, τ_off=0.35, min_silence≈0.25, pad_on≈0.03, pad_off≈0.08`;
onset→snap, offset→protect, interior→transcript (no shift).
