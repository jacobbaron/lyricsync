# Lyric Aligner — Product Spec

**Author:** Jacob
**Status:** Draft v0
**Last updated:** April 2026

---

## 1. Problem

Automatic captioning tools (Instagram, CapCut, Premiere, etc.) run general-purpose ASR on music and produce poor results: missed words, phantom words, and timing errors that concentrate in choruses and fast-rhythm sections. The pain is not the errors themselves — it's the manual repair: every wrong word requires re-typing, and every misaligned timestamp requires dragging start/end points in a subtitle editor.

Forced alignment (audio + known text → word-level timestamps) is a better-posed problem than free-form ASR and has mature open-source tooling (WhisperX, torchaudio's MMS aligner, MFA). No off-the-shelf consumer app wraps this specifically for music videos *and* produces an output format that drops cleanly into a downstream video editor for styling.

We have the lyrics. We want a tool that turns them into timed subtitles, and we want to verify that timing quickly before committing to a styled render.

## 2. Goals

- **G1. Turn a music video + its lyrics into a timed subtitle file** that imports cleanly into any standard editor (Premiere, Resolve, CapCut, `ffmpeg`).
- **G2. Produce a quick "verification" render** — a plain video with the aligned lyrics overlaid on the audio — so we can eyeball alignment in under a minute without exporting from a full video editor.
- **G3. Minimize manual repair work** by (a) isolating vocals before alignment and (b) surfacing low-confidence regions so repair effort is targeted, not exhaustive.
- **G4. Be extensible.** The aligner, separator, and output renderer should be pluggable so we can swap in new models (ElevenLabs Forced Alignment API, MMS, a future custom embedding-based refiner) without rewriting the pipeline.

## 3. Non-goals

- Not a video editor. We output subtitle files and a preview; polished captions happen downstream.
- Not a lyric-transcription tool. We assume lyrics are provided; if they're not, that's a separate problem (and Whisper alone is fine for it).
- Not a karaoke renderer. No per-syllable wipes, no styled templates. Someone else's job.
- Not real-time. Batch pipeline only.
- Not multilingual at v0. English only; design doesn't preclude it.

## 4. Users and use cases

**Primary user:** solo musician / small-band producer who has finished music videos and wants captions for social distribution.

**Representative workflows:**

1. *"Single video, quick turnaround."* User has a finished music video and the lyrics as plaintext. Runs one CLI command, gets an SRT and a verification MP4 in under 5 minutes. Imports SRT into CapCut, applies a style, ships.
2. *"Batch mode."* User has four videos to caption for a release week. Points the tool at a directory; gets all SRTs in one run; spot-checks the verification videos; fixes only the low-confidence sections flagged in the output.
3. *"Tight repair loop."* First-pass alignment has a bad stretch in the bridge. User edits the lyrics file or nudges a few lines' timestamps in the output JSON, re-runs just the render step, re-verifies.

## 5. System architecture

```
┌────────────┐   ┌──────────────┐   ┌──────────────┐   ┌───────────────┐   ┌─────────────────┐
│ Video +    │ → │ Audio        │ → │ Vocal        │ → │ Forced        │ → │ Output          │
│ lyrics.txt │   │ extraction   │   │ separation   │   │ alignment     │   │ generation      │
└────────────┘   │ (ffmpeg)     │   │ (Demucs)     │   │ (WhisperX)    │   │ - SRT/VTT/JSON  │
                 └──────────────┘   └──────────────┘   └───────────────┘   │ - preview MP4   │
                                                                           └─────────────────┘
```

Each stage is a separate module with a well-defined input/output contract. Stages are individually cacheable (by input hash) so re-runs skip work that hasn't changed.

### Stage contracts

| Stage              | Input                        | Output                                  |
|--------------------|------------------------------|-----------------------------------------|
| Audio extraction   | video file                   | `audio.wav` (16kHz mono for alignment)  |
| Vocal separation   | `audio.wav`                  | `vocals.wav`, `accompaniment.wav`       |
| Forced alignment   | `vocals.wav` + `lyrics.txt`  | `alignment.json` (per-word timestamps + confidence) |
| Output generation  | `alignment.json` + `video`   | `captions.srt`, `captions.vtt`, `captions.ass`, `preview.mp4` |

## 6. Functional requirements

### 6.1 Input handling

- Accept any video format `ffmpeg` can read (`.mp4`, `.mov`, `.mkv`, `.webm`).
- Accept audio-only files for the alignment path (`.wav`, `.mp3`, `.flac`, `.m4a`).
- Lyrics as plaintext: one line per caption line, blank lines allowed and preserved as gaps.
- Optional lyrics metadata (JSON): structural hints like `[verse]`, `[chorus]`, or per-line `max_duration` to constrain alignment.

### 6.2 Vocal separation

- Default: `htdemucs` via the `demucs` Python package.
- Configurable model (`htdemucs`, `htdemucs_ft`, `mdx_extra`).
- Skippable via `--no-separation` for a cappella input or debugging.
- Cached by audio-content hash; separation is the slow step and shouldn't re-run unless the audio changes.

### 6.3 Forced alignment

- Default: **WhisperX's `align()` function with a wav2vec2 phoneme model**, called with the provided lyrics as the fixed transcript (not WhisperX's Whisper output). This is the path in m-bain/whisperX issue #939 / #1308.
- Word-level timestamps, aggregated up to line-level using the blank-line structure of the lyrics file.
- Per-word confidence score preserved in the intermediate JSON.
- Aligner is a **plugin**. Interface:
  ```python
  class Aligner(Protocol):
      def align(self, audio_path: Path, lyrics: list[list[str]]) -> AlignmentResult: ...
  ```
  where `AlignmentResult` is a typed structure of `lines[i].words[j].{text, start, end, confidence}`.
- Additional aligners we should stub out as plugins from day one: `ElevenLabsAligner` (API), `MMSAligner` (torchaudio), `MFAAligner` (subprocess).

### 6.4 Output generation

Four deliverables per run:

**(a) `alignment.json`** — canonical intermediate. Per-word timestamps + confidence + line grouping. This is the artifact every downstream consumer reads. Example:
```json
{
  "source": "video.mp4",
  "aligner": "whisperx-wav2vec2",
  "separator": "htdemucs",
  "lines": [
    {
      "text": "when the morning light",
      "start": 12.48,
      "end": 14.22,
      "confidence": 0.94,
      "words": [
        {"text": "when", "start": 12.48, "end": 12.71, "confidence": 0.97},
        {"text": "the", "start": 12.71, "end": 12.84, "confidence": 0.92},
        {"text": "morning", "start": 12.84, "end": 13.56, "confidence": 0.95},
        {"text": "light", "start": 13.56, "end": 14.22, "confidence": 0.93}
      ]
    }
  ]
}
```

**(b) Subtitle files** — derived from the JSON. `SRT` and `VTT` at line granularity (for generic editors); `ASS` with word-level `\k` karaoke tags (for editors that respect them, and for future per-word highlight rendering).

**(c) Preview MP4** — the verification render. Requirements:
- Original video (or a black background if audio-only input) with lyrics overlaid as plain white text on a semi-transparent band.
- Current line shown large; next line shown smaller below, greyed.
- Active word highlighted (e.g., bold or color shift) driven by word-level timestamps.
- Low-confidence words rendered in a warning color (configurable threshold, default `confidence < 0.6`).
- A small timecode HUD in the corner so we can pause and jot down timestamps to fix.
- Rendered with `ffmpeg drawtext` or a direct `moviepy`/`pyav` pipeline — whichever is faster; the preview should render in well under real-time on CPU.

**(d) Summary report** — stdout and a `report.txt`:
```
Lyric Aligner — video.mp4
─────────────────────────
Duration:        3:24
Lines aligned:   42 / 42
Mean confidence: 0.89
Low-confidence lines (< 0.75):
  Line 17 [1:47.3 - 1:49.1] conf=0.61 — "running through the static"
  Line 28 [2:31.9 - 2:33.4] conf=0.58 — "all at once it faded"
Preview:         preview.mp4
Subtitles:       captions.srt, captions.vtt, captions.ass
```

## 7. CLI design

```
lyricsync align video.mp4 lyrics.txt               # basic single-file usage
lyricsync align video.mp4 lyrics.txt \
    --output-dir ./out \
    --aligner whisperx \
    --separator htdemucs \
    --preview-style minimal \
    --confidence-threshold 0.6

lyricsync batch ./videos/ ./lyrics/ --output-dir ./out/
lyricsync render ./out/alignment.json --video video.mp4    # re-render only (no re-alignment)
```

Single command, sensible defaults, every non-trivial choice exposed as a flag. Global `--config path/to/config.yaml` overrides defaults; precedence is CLI flag > config file > built-in default.

## 8. Extension points

Designed so that adding capability is a matter of writing a new class that implements a Protocol and registering it, not touching the core pipeline.

| Extension            | Plugin interface        | Example future additions                    |
|----------------------|-------------------------|---------------------------------------------|
| Audio separator      | `Separator`             | `SpleeterSeparator`, `LALALSeparator`       |
| Aligner              | `Aligner`               | `ElevenLabsAligner`, `MMSAligner`, `MFAAligner` |
| Refiner (post-align) | `Refiner`               | `EmbeddingRepairRefiner`, `ManualEditRefiner` |
| Output formatter     | `OutputFormatter`       | `LRCFormatter`, `YouTubeCaptionsFormatter`, `PremiereMarkerFormatter` |
| Preview renderer     | `PreviewRenderer`       | `MinimalRenderer`, `WaveformRenderer`, `KaraokeRenderer` |

The **refiner stage** is where the "constrained embedding" idea from the earlier design discussion lands. It sits between the aligner and output generation, takes the aligner's `AlignmentResult` plus the original audio, and returns a (hopefully improved) `AlignmentResult`. Initial version: no-op passthrough. Future version: for lines with `confidence < threshold`, re-score candidate alignments using a secondary signal (e.g., semantic similarity between the line's embedding and embeddings of surrounding audio windows transcribed by a separate Whisper pass). Refiners are chainable.

## 9. Quality and evaluation

**Headline metric:** *manual repair rate* — the fraction of words whose timestamps need human adjustment to be acceptable. Measured by hand-labeling a small test set (5–10 songs with diverse tempos and mixing styles) once, then rerunning the pipeline against this set after any change.

**Secondary metrics:**
- Mean absolute timestamp error at word start (ms), against hand-labeled ground truth.
- End-to-end wall-clock time per minute of input audio.
- Confidence calibration: of words flagged `confidence < 0.6`, what fraction are actually wrong? (Calibration matters because the confidence score drives where repair attention goes.)

**Regression harness:** `lyricsync eval ./testset/` runs the pipeline against the labeled set and reports per-song and aggregate metrics. Needed before any refiner work — otherwise we're guessing whether changes help.

## 10. Tech stack

- **Python 3.11+**, managed with `uv`.
- **Core libs:** `whisperx`, `demucs`, `torch`, `torchaudio`, `ffmpeg-python` (or shell out to `ffmpeg` directly).
- **Preview render:** `ffmpeg drawtext` filter for v0 (fastest, fewest dependencies); consider `moviepy` if drawtext's escaping gets painful.
- **Packaging:** single CLI via `typer` or `click`. Installable with `pipx install .`.
- **Config:** `pydantic` for schema, YAML for files.
- **Testing:** `pytest`. Golden-output tests against a small fixture set.
- **CI:** GitHub Actions, lint + unit tests + end-to-end test on a 10-second fixture clip.

## 11. Phasing

**v0 — "end-to-end thin slice" (1 weekend).**
- Single video + lyrics file → SRT + minimal preview.
- No separation (just run WhisperX on raw audio).
- Hardcoded aligner (WhisperX), no plugin scaffolding yet.
- Goal: *is the core alignment good enough to bother with the rest?*

**v1 — "usable."**
- Add Demucs separation; becomes default.
- Add `alignment.json` intermediate + VTT/ASS outputs.
- Add confidence threshold and low-confidence report.
- Introduce plugin Protocols for `Aligner`, `Separator`, `OutputFormatter`.
- Real preview with active-word highlighting and confidence coloring.

**v2 — "extensible."**
- `Refiner` stage, starting with passthrough.
- Second aligner implementation (ElevenLabs API) behind the same interface, to validate the abstraction.
- Batch mode and caching.
- Eval harness + labeled test set.

**v3+ — ideas, not commitments.**
- Embedding-based refiner for low-confidence regions.
- Interactive repair CLI (`lyricsync repair alignment.json`) — terminal UI with scrub + per-word nudge.
- Web-based repair UI.
- LRC export for music players.
- Integration with Premiere / Resolve via marker files.

## 12. Risks and open questions

- **WhisperX alignment on singing.** WhisperX's wav2vec2 phoneme model is trained on speech. On isolated vocals it's usually fine; on genre-specific vocal styles (heavy vibrato, melisma, screamed vocals) it may degrade. Mitigation: stem separation helps; if it's still bad, MMS or MFA as fallback aligners.
- **Phoneme-model vocabulary gaps.** WhisperX replaces unknown characters with wildcards; heavy punctuation or stylized lyrics ("yeaaaaah") may confuse it. Mitigation: lyric-file preprocessor that normalizes spelling and lets the user keep a stylized version for display separately from the alignment version.
- **Demucs wall-clock time on CPU.** A 3-minute song takes a couple of minutes on CPU with `htdemucs`. Acceptable for a personal tool; if it's annoying, add GPU detection.
- **Preview render quality vs. speed tradeoff.** `ffmpeg drawtext` is fast but ugly; a prettier preview takes longer. v0 should stay ugly-but-fast; pretty is a solved problem once the export stage exists.
- **Do we need a GUI?** Open. If the CLI + SRT-to-CapCut flow is smooth enough, probably never. If repair turns out to be the bottleneck, a targeted repair UI (not a full editor) is the right next thing to build.

## 13. Success criteria

This project is done (at v2) if:
1. Captioning a new music video takes under 10 minutes of wall-clock time end-to-end, including any repair.
2. On the labeled test set, at least 90% of word timestamps are within 150ms of ground truth without any manual repair.
3. The SRT output drops into CapCut / Premiere / `ffmpeg` without any massaging.
4. Adding the ElevenLabs aligner required no changes outside the `aligners/` directory.
