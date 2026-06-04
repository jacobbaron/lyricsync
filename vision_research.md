# Vision Support for Story Generation — Research & Design

**Status:** Research / proposal (no code changes yet)
**Date:** 2026-06-04
**Goal:** Let the story generator *see* the footage, not just read the
transcript, so cuts can be driven by what's happening on screen — not only the
spoken word.

---

## 1. Where we are today

The current pipeline is **audio/text-only**:

```
upload clips → Whisper transcribe (word timestamps)
            → WhisperX align + merge onto a global timeline (merged.json)
            → Claude picks cuts  ←★ sees ONLY format_transcript(words) text
            → resolve quotes → timestamps → ffmpeg splice
```

The decisive moment is in `modal/app.py::_generate_worker`. Claude
(`claude-opus-4-5`) is handed a plain-text transcript built by
`transcript.py::format_transcript()` — grouped by source clip, split into
paragraphs on >1.5s gaps, **no timestamps, no visual information**. It returns
verbatim quotes, which `resolve_segments()` maps back to word-level
timestamps. ffmpeg (`_render_worker`) then trims and concatenates those ranges.

**The gap:** Claude has no idea who is on camera, what they're doing, where
they're looking, whether the shot is wide or tight, whether there's a reaction,
a laugh, a gesture, an action beat, a scene change, or dead air. A
visually-dramatic moment with no dialogue is *invisible* to it today. So is
the difference between "great take, person centered and animated" and "same
words, but they're off-frame fumbling with the mic."

This doc surveys what's possible, what it costs, and a recommended path that
slots into the existing architecture with minimal disruption.

---

## 2. The three architectural options

### Option A — Native video model analyzes the whole session
Feed the actual video (frames + audio) to a model with native video
understanding (Gemini 2.5 / 3 family) and let *it* propose the cuts directly.

- **Pros:** Richest understanding; temporal continuity; model sees motion,
  reactions, framing, scene changes natively. One call replaces transcript +
  vision.
- **Cons:** Replaces our Claude story-picker and the whole quote→timestamp
  mechanism. Larger rewrite. Less control over the deterministic
  quote-matching we already trust. Vendor lock to one model for the creative
  step.

### Option B — Sample frames as images into a multimodal LLM
Extract frames (e.g. 1 every 2–5s, or per shot), send them as images to a
vision LLM (Claude Opus/Sonnet are multimodal; GPT-class too) interleaved with
the transcript, and have it pick cuts.

- **Pros:** Keeps Claude as the creative brain. We control sampling density.
- **Cons:** Images are the *expensive* token format (see §3). Loses motion
  between sampled frames. Lots of frames = lots of tokens. Awkward to align
  many images to many timestamps in the prompt.

### Option C — Cheap "visual transcript" stage, then the existing picker ★ RECOMMENDED
Add **one new pipeline stage** that turns each clip's video into a compact,
**timestamped visual track** ("shot log" / "visual transcript") using a cheap
native-video model and/or local shot detection. Merge that visual track into
the same global timeline as the words, and feed *both* to the existing Claude
picker as text.

```
            → Whisper + WhisperX (unchanged)
NEW         → Visual analysis per clip  →  timestamped visual events
            → merge words + visual events onto the global timeline
            → Claude picks cuts  ←★ now sees dialogue AND a visual track
            → resolve → timestamps → ffmpeg (unchanged)
```

- **Pros:** Smallest change to a working system. Claude stays the creative
  brain and keeps the deterministic quote→timestamp safety net. The visual
  track is cheap to produce, cacheable, reusable across every generation round
  (we already do multi-round conversations). Model-agnostic — swap the
  visual-analysis model without touching the picker.
- **Cons:** Information bottleneck: the picker sees a *text description* of the
  video, not the video. Quality depends on the visual-transcript prompt.
- **Why it wins here:** Our cut-selection already runs on *text quotes resolved
  to timestamps*. A visual track is just another text input on the same
  timeline. It composes with everything we've built (rounds, quote matching,
  render) instead of replacing it.

---

## 3. Cost analysis (current 2026 pricing)

Two things dominate cost: **how video is tokenized** and **which model**.

### Native video tokenization (Gemini)
Gemini samples video at **1 fps** by default and charges roughly:
- **~300 tokens/sec** at default media resolution (258 tok/frame + 32 tok/sec audio + metadata)
- **~100 tokens/sec** at *low* media resolution (66 tok/frame) — "minimal perf drop"

### Image tokenization (Claude / frame-sampling)
Claude image tokens ≈ `(width × height) / 750`. A 1280×720 frame ≈ **~1,230
tokens each** — i.e. one HD frame costs ~12× a single default Gemini video-frame.

### Model rates (input / output per 1M tokens, 2026)
| Model | Input | Output |
|---|---|---|
| Gemini 2.5 Flash-Lite | $0.10 | $0.40 |
| Gemini 2.5 Flash | $0.30 | $2.50 |
| Gemini 2.5 Pro | $1.00–1.25 | $10.00 |
| Claude Sonnet 4.6 | $3.00 | $15.00 |
| Claude Opus 4.7 | $5.00 | $25.00 |

### Worked example — a 10-minute (600s) recording session

| Approach | Tokens (input) | Input cost |
|---|---|---|
| **Gemini Flash, low-res native video** (Option A/C) | ~60k | **~$0.02** |
| **Gemini Pro, default-res native video** (Option A) | ~180k | ~$0.18–0.22 |
| **Gemini Flash-Lite, low-res** (Option C, cheapest) | ~60k | **~$0.006** |
| **Claude Sonnet, frame sampling @0.5 fps, 720p** (Option B) | ~370k | ~$1.11 |
| **Claude Opus, frame sampling @1 fps, 720p** (Option B) | ~740k | ~$3.69 |
| **PySceneDetect shot boundaries** (local ffmpeg) | — | **$0 (compute only)** |

**Takeaways**
- Native video (Gemini) is **50–200× cheaper** than pushing frames through a
  premium multimodal LLM, because video frames tokenize ~12× cheaper than HD
  images *and* the cheap models are far cheaper per token.
- The visual analysis is a **one-time cost per clip** — cache it. Our flow
  re-generates stories across many rounds; the visual track is computed once
  and reused every round for free.
- For typical sessions (a few minutes to ~an hour) the visual-analysis stage
  costs **cents**. Cost is not the constraint; *prompt/representation quality*
  is.

---

## 4. Recommended approach (Option C, in detail)

Produce a **timestamped visual track** per clip, merge it with the word
timeline, and expose it to the existing Claude picker.

### 4a. What the visual track contains
A list of timestamped events / shot descriptions, e.g.:

```json
{
  "source": "IMG_2415.mov",
  "shots": [
    {"start": 0.0,  "end": 8.4,  "shot": "wide",
     "desc": "Two people seated at a table, laughing; person on left gestures."},
    {"start": 8.4,  "end": 15.2, "shot": "medium",
     "desc": "Person on right leans in, animated, makes eye contact with camera."},
    {"start": 15.2, "end": 19.0, "shot": "wide",
     "desc": "Both go quiet; person on left looks away — a beat / pause."}
  ],
  "highlights": [
    {"t": 12.6, "kind": "reaction", "desc": "Genuine laugh"},
    {"t": 31.2, "kind": "action",   "desc": "Stands up, walks to window"}
  ]
}
```

Two complementary signals:
1. **Shots** — boundaries + a one-line description and framing per shot.
2. **Highlights** — point-in-time visual beats (reactions, gestures, actions,
   eye contact, scene changes) that make good in/out points or punctuation.

### 4b. How to produce it (cost-effective)
A hybrid that pairs cheap local detection with a cheap vision model:

1. **Shot boundaries — local, free.** Run **PySceneDetect** (content detector)
   over each clip. We already have `ffmpeg` in the Modal images, and PySceneDetect
   is a thin ffmpeg/OpenCV wrapper. This gives precise shot cut timestamps for $0
   and reduces how much we hand to a model.
2. **Descriptions — cheap native video.** Send the clip (low media resolution,
   audio off — we already have the words) to **Gemini 2.5 Flash / Flash-Lite**
   and ask for the shot/highlight JSON above, anchored to timestamps. Pennies
   per clip. Optionally pass the PySceneDetect boundaries in the prompt so the
   model describes *within* known shots instead of inventing them.

This keeps everything in the existing Modal worker model (download from R2 →
process → write JSON to R2 → update DB), exactly like `_transcribe_worker`.

### 4c. How it changes the cut selection
Extend `format_transcript()` (or add a sibling) so each source section
interleaves visual context with the spoken paragraphs on the same clock, e.g.:

```
=== IMG_2415.mov ===
[wide — two people at a table, laughing]
so anyway that's when we decided to just go for it
[medium — she leans in, animated, looks at camera]
and honestly it was the best decision we ever made
[wide — both go quiet, he looks away ~3s pause]   ← visual beat, no dialogue
[reaction @ +12.6s: genuine laugh]
```

Then update `_SYSTEM_PROMPT` and `_PROPOSE_STORIES_TOOL` so Claude can:
- weight cuts toward visually strong takes (centered, animated, good framing),
- use **visual-only beats** (a laugh, a reaction, an action, a landscape) as
  segments even when nothing is said,
- cut on shot boundaries for cleaner edits,
- build a **visual arc** (establish → build → payoff), not just a verbal one.

Because the picker still returns verbatim **quotes**, the existing
`resolve_segments()` safety net is unchanged for dialogue. For **visual-only
segments** we need a small extension (see §5) since there's no quote to match —
the model would reference a shot/highlight by its timestamp or id instead.

---

## 5. Implementation sketch

Minimal, phased, additive. Nothing below removes existing behavior.

**New Modal worker** `analyze_visuals(project_id)` (mirror `_transcribe_worker`):
- For each clip: download from R2 → PySceneDetect for boundaries → Gemini
  Flash for shot/highlight JSON → upload `clips/<id>/visual.json` to R2.
- Add to the existing align/merge step: fold visual events into the global
  timeline (they already have local timestamps + `global_start` offset, same as
  words) and write into `merged.json` (or a parallel `visual_merged.json`).

**Schema** (small):
- `clips.visual_r2_key text` — pointer to per-clip visual JSON.
- New project status `analyzing_visuals` (between `transcribed` and
  `stories_ready`), or run it in parallel with transcription.
- For visual-only cuts: stories' `ranges_json` already carries `{source, start,
  end, text}`. A visual segment is just a range with `start/end` from a shot
  and a `kind: "visual"` marker — `_render_worker` already trims by
  `start/end`, so **render needs no change**.

**Prompt/tool changes** in `modal/app.py`:
- `format_transcript()` → interleave visual context (above).
- `_SYSTEM_PROMPT` → teach the editorial use of visual signal.
- `_PROPOSE_STORIES_TOOL` → allow a segment to be either a `quote` (dialogue,
  resolved as today) **or** a `visual_ref` (source + shot/highlight id/time
  range, resolved directly to timestamps — no fuzzy matching).
- `resolve_segments()` → handle the `visual_ref` branch.

**Secrets:** add `GEMINI_API_KEY` (or `GOOGLE_API_KEY`) to `lyricsync-secrets`;
add `google-genai` to the analysis image's `pip_install`.

**Dependencies:** `scenedetect[opencv]` for the analysis image.

### Suggested phasing
1. **MVP / spike:** `analyze_visuals` writes `visual.json`; interleave into
   `format_transcript`; update prompt. *No schema/tool change yet* — Claude
   still outputs dialogue quotes, but now chooses them with visual awareness.
   This alone should noticeably improve take selection and is a 1–2 file change.
2. **Visual-only segments:** add the `visual_ref` tool branch + resolver so
   purely visual beats can become cuts (b-roll, reactions, scenery).
3. **Tuning:** shot-boundary-aware trim points; `media_resolution` / fps knobs;
   per-project "style" prompts ("favor energetic, tight cuts" vs "slow,
   observational").

---

## 6. Risks & caveats
- **Representation bottleneck (Option C):** the picker reads a *description*.
  If the visual prompt is vague, cuts won't improve. Invest in the
  visual-transcript prompt; consider a couple of golden clips as a regression
  check.
- **Timestamp alignment:** visual events must ride the same `global_start`
  offset machinery as words (see `_align_worker`). Clips without creation-time
  metadata fall back to `global_start=0`, which already limits cross-clip sync —
  visual events inherit the same limitation.
- **Vendor mix:** introduces Gemini alongside OpenAI (Whisper) and Anthropic
  (picker). Acceptable — each is best-in-class and cheapest for its job — but
  it's a third API key/secret to manage.
- **Latency:** native video analysis is fast and parallelizable per clip, but
  it's a new stage; run it concurrently with/after transcription so it doesn't
  serialize the critical path.
- **If we'd rather not add Gemini:** Claude Opus/Sonnet *are* multimodal — we
  can do the same visual-transcript stage with sampled frames (Option B
  mechanics, but producing a cached text track, not picking cuts). Costs more
  (§3) but keeps a single vendor. Native video is the cost-effective default.

---

## 7. Recommendation in one line
Add a **cached, timestamped visual-transcript stage** (PySceneDetect for cut
points + Gemini 2.5 Flash for descriptions, ~cents per session), **merge it
into the existing global timeline**, and let the **current Claude picker** read
dialogue *and* visuals — starting with an MVP that needs no schema or tool
changes, then adding visual-only cuts. This buys a video-driven storyline while
preserving the deterministic quote→timestamp render path we already trust.

---

## Sources
- [Gemini video understanding — token counting](https://ai.google.dev/gemini-api/docs/video-understanding)
- [Gemini API pricing](https://ai.google.dev/gemini-api/docs/pricing)
- [Low media resolution: 258 → 66 tokens/frame](https://x.com/AntoineYang2/status/1920840072804380935)
- [Claude API pricing](https://platform.claude.com/docs/en/about-claude/pricing)
- [Frame dedup + scene detection for video-LLM cost reduction](https://dev.to/pritom14/how-i-built-video-token-optimization-for-vision-llms-cutting-costs-13-45-with-frame-dedup-scene-2ic)
- [Scene detection policies & keyframe extraction strategies (arXiv 2506.00667)](https://arxiv.org/pdf/2506.00667)
- [Shot-aware frame sampling for video understanding (arXiv 2603.17374)](https://arxiv.org/pdf/2603.17374)
</content>
</invoke>
