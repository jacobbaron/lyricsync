# LLM Video Editor — Capability Roadmap

Direction: evolve lyricsync from "Claude picks quotes, ffmpeg splices them" into a
fully featured video editor that an LLM drives through the API. The LLM should be
able to *see* better, *edit* with finer tools, *check its own work*, and iterate
*fast and cheap*.

Current baseline (for context):

- **Perception**: merged word-level transcript (Whisper + WhisperX) as plain text;
  experimental Gemini visual analysis (`visual_analyses` variants — segments,
  highlights, suggested clips) not yet wired into generation.
- **Editing**: verbatim-quote → timestamp range resolution, segment reordering
  across clips, optional title-card drawtext overlay.
- **Rendering**: single ffmpeg `filter_complex` pass on Modal → 1080x1920 mp4.
- **Loop**: generate → render → human watches. No machine-readable feedback on
  the rendered output.

The directions below are grouped into five tracks. Each item has a high-level
implementation plan. Items are largely independent; rough suggested ordering is
at the end.

---

## Track 1 — Perception (help the LLM see what's in the footage)

### 1.1 Promote visual analysis into the generation context

The VIS-01 harness already produces shot segments, highlight beats (expression,
tone, gesture), and suggested clips — but `generate_stories` only sees the
transcript. This is the single highest-leverage perception change.

**Plan**

1. Pick the winning variant from the A/B harness (likely `with_transcript` or
   `audio_aware`) and make it the canonical analysis, run automatically after
   transcription (chain it in the `transcribe_clip` → `analyze_visuals` flow).
2. Define a compact "perception document" per clip: transcript words interleaved
   with visual beats on one timeline (e.g. `[00:12.3] (close-up, laughing) "no
   way, that actually worked"`). Build it in `modal/transcript.py`-style pure
   helpers so it's testable.
3. Inject the perception document into the `generate_stories` prompt in place of
   the bare transcript; extend the story tool schema so Claude can reference
   visual beats (not just quotes) as cut points — e.g. `{"source": ...,
   "highlight_id": ...}` as an alternative to `{"quote": ...}`.
4. Keep the debug blob discipline: store the exact perception doc used per
   generation round for later evaluation.

### 1.2 Face detection & tracking track

Know who is on screen, where, and when. Enables "cut to Sarah's reaction",
auto-reframing (1.4), and privacy blurring later.

**Plan**

1. New Modal function `detect_faces` on a CPU/lightweight-GPU image using a
   local model (e.g. YOLO-face or MediaPipe/InsightFace) sampling at ~2–5 fps;
   output per-frame boxes + tracked identities (`person_0`, `person_1`, …) with
   embeddings for cross-clip re-identification.
2. Store as `faces.json` in R2 next to the transcript (`projects/{p}/clips/{c}/faces.json`)
   plus a summary row (people count, screen-time per identity) in a new
   `face_tracks` table or inside `visual_analyses.result`.
3. Optionally label identities via one Gemini call ("person_0 is the man in the
   red shirt") so the LLM can use stable human-readable names.
4. Merge face presence into the perception document (1.1): `(person_0 on
   screen, smiling)` annotations per timeline span.

### 1.3 Frame / time-range inspection tools (interactive perception)

Today perception is all precomputed. Give the editing LLM on-demand tools to
look closer when it's unsure — the same way a human editor scrubs.

**Plan**

1. New API endpoints backed by small Modal functions:
   - `GET /api/clips/{id}/frames?t=12.5&n=5&interval=0.5` → extract frames with
     ffmpeg, return signed R2 URLs (cache by `(clip, t, n, interval)`).
   - `POST /api/clips/{id}/describe` with `{start, end, question}` → Gemini Flash
     on just that sub-range (ffmpeg `-ss/-to` trim → upload → ask).
   - `GET /api/clips/{id}/contact-sheet?start&end` → single tiled image
     (e.g. 4x4 thumbnails with timestamps burned in) — one image instead of 16,
     ideal for multimodal LLM consumption.
2. Expose these as tools in the `generate_stories` agent loop (it's already
   multi-turn with the Anthropic SDK) so Claude can call them mid-generation.
3. Aggressively cache in R2; frames and contact sheets are immutable per clip.

### 1.4 Audio perception beyond words

Energy, laughter, music, silence — pacing signals the transcript misses.

**Plan**

1. In the transcription worker (audio already extracted), compute a cheap
   per-second feature track: RMS loudness, silence spans, and optionally an
   audio-event classifier (laughter/music/applause via a small model like YAMNet
   or PANNs CPU inference).
2. Store as `audio_features.json`; surface notable events (laughter bursts,
   long silences, music start) in the perception document (1.1).
3. Later: use silence spans for smart cut snapping (Track 2.1) so cuts land in
   natural pauses instead of mid-breath.

---

## Track 2 — Editing tools (a richer edit vocabulary)

### 2.1 Timeline edit model: from quotes to an EDL

The pivotal refactor. Replace `ranges_json` quote-lists with a proper edit
decision list (EDL) the LLM manipulates with explicit operations — the
foundation everything else in this track plugs into.

**Plan**

1. Define a versioned JSON timeline schema:
   ```json
   {
     "version": 1,
     "fps": 30, "width": 1080, "height": 1920,
     "tracks": [
       {"type": "video", "items": [
         {"clip_id": "...", "src_start": 12.4, "src_end": 18.2,
          "speed": 1.0, "transition_in": {"type": "crossfade", "dur": 0.3},
          "crop": null}
       ]},
       {"type": "text", "items": [ ... ]},
       {"type": "audio", "items": [ ... ]}
     ]
   }
   ```
2. Store it on the story row (`stories.timeline_json`), keep `ranges_json`
   readable as a legacy import (quote-resolution becomes the function that
   *produces* timeline items).
3. Add edit-op endpoints (or one `POST /api/stories/{id}/edit` accepting a list
   of ops): `trim`, `split`, `move`, `delete`, `set_speed`, `add_text`,
   `set_transition`, `nudge_to_silence` (snap boundary to nearest pause from
   1.4). Each op validates against the schema and bumps a revision counter.
4. Render reads the timeline, not ranges. Keep the compiler from timeline →
   ffmpeg filtergraph as a pure, unit-tested module.
5. Every edit appends to a `story_revisions` table (timeline snapshot + op) —
   undo/redo for free, and a training/eval record of how edits evolve.

### 2.2 Pretty text: styled & animated captions and titles

Replace raw `drawtext` with a real typography layer — fonts, outlines, boxes,
word-by-word karaoke captions, animated entrances.

**Plan**

1. Adopt **libass/ASS subtitles** as the text engine (ffmpeg renders `.ass`
   natively, supports per-word karaoke timing `\k`, fades, transforms, fonts,
   outlines, positioning) — big win without leaving ffmpeg.
2. Build a small "style preset" library (e.g. `hormozi`, `clean-lower-third`,
   `karaoke-bounce`, `minimal-serif`) compiled from timeline text items + word
   timings into ASS markup. The LLM picks `{preset, text or "auto-captions",
   position, t_start, t_end}`; it never writes raw ASS.
3. Auto-captions mode: take word timings already in the aligned transcript for
   the selected ranges and emit word-synced caption events (the data is already
   exact — this is nearly free).
4. Ship fonts in the Modal render image; add `fontconfig` + a `assets/fonts/`
   directory.
5. For motion-graphics-grade animation beyond ASS, see 2.4 (Remotion track).

### 2.3 Smart reframing & zoom (uses face track)

Vertical 1080x1920 output from arbitrary source framing: keep the speaker
centered, punch in on reactions.

**Plan**

1. Use face tracks (1.2) to compute a smoothed crop window per timeline item
   (simple Kalman/EMA smoothing of the dominant face's center; fall back to
   center crop when no faces).
2. Expose as timeline item fields: `crop: "auto-face" | {x,y,w,h}` and a
   `zoom` keyframe list (`[{t, scale}]`) so the LLM can request "slow punch-in
   on the laugh at 14.2s".
3. Compile to ffmpeg `crop` + `zoompan`/`scale` expressions in the timeline
   compiler.

### 2.4 Remotion (or similar) compositing lane for motion graphics

For animated titles, progress bars, emoji pops, B-roll layouts that exceed what
ffmpeg/ASS can express.

**Plan**

1. Keep ffmpeg as the base renderer. Add an optional overlay pass: a Remotion
   project in the repo with a few parameterized compositions (animated title,
   stat callout, emoji reaction); the timeline's text/graphic items with
   `engine: "remotion"` get rendered to transparent WebM/ProRes on a Node Modal
   image, then ffmpeg overlays them.
2. Composition props are plain JSON — exactly the right shape for an LLM tool.
3. Defer until 2.2 proves insufficient; ASS covers a surprising amount.

---

## Track 3 — Self-evaluation (close the loop)

### 3.1 Render review: LLM watches its own output

After every render, automatically produce a critique the editing LLM (and the
user) can act on.

**Plan**

1. Post-render Modal step: upload `output.mp4` to Gemini Flash with a rubric
   prompt ("rate hook strength in first 2s, pacing, cut smoothness, caption
   legibility; list timestamped issues") → structured JSON verdict.
2. Store on the story (`stories.review_json`) and expose
   `GET /api/stories/{id}/review`.
3. Feed the review back into the next `generate`/`edit` round automatically:
   "your previous cut was rated X because Y — fix it". This turns
   generate→render→review into an agentic loop with a stopping criterion
   (score threshold or max iterations).

### 3.2 Mechanical lint pass (cheap, deterministic checks)

Catch objective defects without burning model tokens.

**Plan**

1. Pure-Python checks over the timeline + assets before render: segments that
   start/end mid-word (compare against word timings), sub-300ms fragments,
   duplicate ranges, text overlapping faces (face boxes ∩ caption box), cuts
   not in silence when a nearby pause exists, total duration vs. target.
2. Post-render ffprobe/ffmpeg checks: black frames, frozen frames
   (`freezedetect`), audio clipping/loudness (`ebur128` vs. -14 LUFS target),
   A/V sync drift.
3. Return lint results from the edit endpoint itself (fast feedback) and block
   render on `error`-severity findings.

### 3.3 Eval harness for editing quality

You already A/B prompt variants for perception; do the same for editing.

**Plan**

1. Curate a small fixture set of projects with "known good" cuts (human-picked).
2. Score generated timelines against references (range IoU, hook overlap) plus
   the 3.1 rubric score; record per prompt/model/perception-variant in a table
   mirroring `visual_analyses`' debug discipline.
3. Run on demand when changing prompts/models — regression safety for prompt
   engineering.

---

## Track 4 — Efficiency (cheaper tokens, less waste)

### 4.1 Compact, hierarchical perception context

Full word-level transcripts + visual beats for many clips will blow up context
and cost. Give the LLM a zoom hierarchy instead of everything at once.

**Plan**

1. Generate per-clip summaries (one paragraph + key moments list) at ingest —
   the existing `context` Gemini variant is exactly this.
2. Generation prompt gets: project-level summary → per-clip summaries → and
   *tools* to pull full transcript/perception detail for a specific clip or
   time range on demand (pairs with 1.3).
3. Cache-friendly prompt layout: static instructions and per-clip docs ordered
   stably so Anthropic prompt caching hits across rounds of the multi-turn
   generation conversation.

### 4.2 Right-size models per task

**Plan**

1. Keep Opus/extended-thinking for creative story selection; route mechanical
   tasks (lint explanation, caption styling choices, review summarization) to
   Haiku/Flash-class models.
2. Make model choice a per-function config (env/DB) so the eval harness (3.3)
   can measure cheap-vs-expensive quality deltas before downgrading anything.

### 4.3 Proxy media pipeline

Never make a model (or a preview render) touch original 4K files.

**Plan**

1. At ingest, generate once per clip: 480p proxy mp4, audio-only m4a, thumbnail
   strip / contact sheets, waveform PNG. Store in R2 under the clip prefix.
2. All perception (Gemini, faces) and preview renders read proxies; only final
   render touches originals. Gemini token cost scales with resolution/fps —
   `flash_lowres` results should confirm the quality is fine.

---

## Track 5 — Speed (faster iteration loop)

### 5.1 Draft-quality preview renders

The LLM (and user) shouldn't wait for a full-quality encode to evaluate a cut.

**Plan**

1. Add `quality: "draft" | "final"` to the render endpoint. Draft = proxies as
   input, 480x854, `ultrafast` preset, CRF 30+ — typically 5–10x faster.
2. The self-eval loop (3.1) runs on drafts; only a passing timeline gets a
   final render.
3. Optionally smaller: `POST /api/stories/{id}/preview?start&end` renders just
   a slice of the timeline for spot-checking one transition.

### 5.2 Parallel + cached rendering

**Plan**

1. Split the timeline into per-item encode jobs (`Function.map` on Modal), then
   concat the pieces (`-f concat -c copy` when codecs match) — wall-clock time
   becomes the longest single segment.
2. Cache encoded segments in R2 keyed by hash of (clip, in/out, speed, crop,
   text overlays, quality). Editing one segment of a 12-segment story re-encodes
   one segment; everything else is a cache hit. This is the biggest speed win
   for the *iterative editing* loop, where most of the timeline is unchanged
   between revisions.

3. Keep source-clip download caching (already present) and add a warm Modal
   volume for proxies.

### 5.3 GPU encode where it pays

**Plan**

1. Benchmark NVENC (`h264_nvenc`) on Modal GPU containers vs. libx264 on big
   CPU containers for the final 1080x1920 encode; switch the final-render image
   if the price/perf wins. Draft renders stay CPU (`ultrafast` is usually
   enough).

---

## Suggested sequencing

1. **2.1 Timeline/EDL model** — the foundation; everything else attaches to it.
2. **1.1 Visual analysis into generation** + **4.1 compact context** — biggest
   immediate quality lift, mostly prompt + plumbing work.
3. **2.2 ASS captions/titles** + **5.1 draft renders** — visible polish + fast
   loop, both cheap to build.
4. **3.1 render review + 3.2 lint** — closes the agentic loop.
5. **1.2 faces → 2.3 auto-reframe**, **5.2 parallel/cached render**.
6. **2.4 Remotion lane**, **5.3 GPU encode**, **3.3 eval harness** as the
   system matures.
