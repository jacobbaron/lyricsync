# Discovery: Analysis-tool inventory, LLM-variant consolidation, and auto-backfill

Status: **discovery / proposal** — no implementation yet.

Goals (from the ticket):

1. Maintain a clear, single list of every analysis tool: what it is, what it's
   for, and how much of the library is backfilled.
2. **Simplify the LLM tooling** — the Gemini variant list is confusing; nobody
   knows which one to use when. Deprecate the redundant ones; ideally converge
   on ONE LLM tool that combines everything (large + small videos, transcript
   AND audio, drill-down, editorial suggestions).
3. **Improve discoverability for agents** using the API — when an agent is
   asked to make a cut (or searches / requests clip info), it should be obvious
   which perception results exist and how to get them, ideally presented
   automatically.
4. **Ensure tools that are used get backfilled automatically** and stuck tasks
   get unstuck.

---

## 1. Inventory — every analysis tool today

Coverage numbers are a live snapshot (2026-07-02) over **76 clips** in 11
projects (~50 clips in "real" projects; the rest are test uploads).

### 1a. LLM (Gemini) visual analyses — `visual_analyses` table

One row per (clip, variant) run. Trigger: `POST /api/clips/{id}/analyze
{variant}`; results: `GET /api/clips/{id}/visual`. Registry:
`VISUAL_VARIANTS` in `modal/app.py`; prompts in `modal/visual.py`.

| variant | model / config | what it does | done | error | stuck |
|---|---|---|---|---|---|
| `context` | flash, low-res | 1–3 sentence summary → written to `clips.visual_description` | 23 | 1 | 0 |
| `flash` (API default) | flash | visual-only segments + highlights | 3 | 2 | 5 |
| `flash_lowres` | flash, low-res | same, cheaper — was an A/B cost probe | 3 | 2 | 4 |
| `pro` | 2.5-pro | quality ceiling — **already disabled in modal/app.py** | 1 | 3 | 4 |
| `editorial` | flash | + `suggested_clips` (ready-to-cut moments) | 3 | 3 | 4 |
| `audio_aware` | flash, high-res, 3fps | audio+video; expression/tone reads | 1 | 0 | 0 |
| `with_transcript` (**canonical**) | flash, high-res, 3fps | audio+video grounded on aligned transcript; auto-run post-align; feeds story generation | 30 | 3 | 0 |
| `grounded` | = with_transcript + T1/T2 signals in prompt | A/B: grounded vs ungrounded | 1 | 0 | 0 |

17 rows are stuck in `analyzing` for >1h (nothing ever times them out).

### 1b. Deterministic perception signals — `clip_signals` table (PERCEPTION T1–T5)

No LLM; computed on Modal. Trigger: `POST /api/clips/{id}/{quality,motion,detect,embed}`;
results: `GET /api/clips/{id}/signals?kind=…`. Full sidecar JSON in R2.

| kind | what it does | why it matters | clips done |
|---|---|---|---|
| `quality` (T1) | per-second sharpness / exposure / shake / frozen → flagged unusable spans | avoid cutting on bad footage; grounds the LLM (T3) | **1 / 76** |
| `camera_motion` (T2) | optical-flow pan/tilt/zoom/whip/handheld spans + in-camera scene cuts | natural cut points, pacing; grounds the LLM (T3) | **3 / 76** |
| `embedding` (T4) | CLIP ViT-B-32 per-frame + pooled vectors → pgvector | **semantic search** `GET /api/projects/{id}/search?q=…` | **3 / 76** |
| `detection` (T5) | YOLO boxes → IoU tracklets → per-class inventory (count, screen time) | "is there a person/cat/drill on screen at t=…" | **2 / 76** |

### 1c. Interactive drill-down tools (roadmap §1.3) — on-demand, cached

Not backfillable state — these run per request and cache by params:

- `GET /api/clips/{id}/contact-sheet?start&end&cols&rows` — tiled frames w/ timestamps
- `GET /api/clips/{id}/frames?t&n&interval` — exact frames
- `POST /api/clips/{id}/describe {start, end, question?}` — Gemini Flash on a sub-range

### 1d. Audio analysis

- Aligned word-level transcript (Whisper + WhisperX) — full coverage on real projects
- `audio_analysis.json` (waveform peaks + Silero VAD speech intervals) — powers the editor UI waveform

---

## 2. Problems

1. **Variant sprawl.** 8 registered variants; 5 of them (`flash`,
   `flash_lowres`, `pro`, `editorial`, `audio_aware`) were A/B experiments from
   the VIS-01 harness that never got retired after `with_transcript` won and
   became canonical. The API default is still `flash` — i.e. calling the
   endpoint with no body runs a *non-canonical experiment variant*.
2. **Two runs where one would do.** `context` exists only to populate
   `clips.visual_description`, but the canonical run already produces a
   `summary` — we pay for a second Gemini call to store a worse summary.
3. **No lifecycle management.** 17 rows stuck `analyzing` forever; errors are
   never retried; there is no sweep that notices a clip missing its canonical
   analysis (auto-run only fires on the align path — clips uploaded before the
   feature, or where the spawn failed, are permanently missing).
4. **Signals virtually unbackfilled** (1–3 clips each), so T3 grounding and
   semantic search basically don't work library-wide.
5. **Discoverability is documentation-only.** An agent must have read CLAUDE.md
   to know the tools exist. Clip/project API responses don't say what analyses
   exist, don't include the results, and don't advertise how to request missing
   ones. The OpenAPI doc (`/api/openapi.json`) covers only part of the surface.

---

## 3. Proposal

### 3.1 Consolidate to ONE canonical LLM tool: `analyze`

Merge everything the variants do into a single pass — essentially
`with_transcript` + `editorial` + `store_summary`:

- **Model/config:** gemini flash, high media resolution, 3 fps (current
  canonical config).
- **Inputs:** video + audio always; aligned transcript when available
  (degrade gracefully to audio-only when not — covers pre-transcription runs).
  No signal grounding (see `grounded` deprecation below).
- **Output (superset schema):** `summary` (→ stored on
  `clips.visual_description`), `segments`, `highlights` (with
  `expression`/`tone`), and `suggested_clips`.
- **Large videos:** for clips over a duration threshold (e.g. >5 min), chunk
  into overlapping windows server-side, analyze per window, merge on the
  clip-local timeline. (Today long clips just get coarse output.)
- **Drill-down:** stays as the §1.3 interactive tools (`describe`, `frames`,
  `contact-sheet`) — the unified tool doesn't need to subsume them, it needs to
  *point at them* (see 3.3).

API: `POST /api/clips/{id}/analyze` with **no variant param** (or
`variant: "v2"` internally for provenance). One analysis kind per clip; re-runs
supersede.

### 3.2 Deprecations

| variant | fate | rationale |
|---|---|---|
| `pro` | **delete** | already disabled; ~10× cost for marginal gain |
| `flash`, `flash_lowres` | **delete** | A/B probes; superseded by canonical |
| `editorial` | **fold in** | `suggested_clips` becomes part of the unified output |
| `audio_aware` | **fold in** | unified tool degrades to this when no transcript exists |
| `context` | **fold in** | summary comes free from the unified run |
| `grounded` | **delete** | A/B'd on 4 clips (2026-07-02): mild segment-boundary gains, equivalent highlights, and the quality signal over-flags handheld footage ("shake" on 60–100% of clip duration) so grounding it adds noise. Decision: not worth the T1/T2-before-LLM ordering dependency. |
| `with_transcript` | **becomes** the unified tool | |

Migration: keep old rows readable (readers already fall back to "newest done
analysis of any variant"); remove the variants from the API enum + OpenAPI;
map old names to the unified tool for one deprecation window (202 + warning
header) rather than 400ing existing scripts.

### 3.3 Discoverability for agents

1. **Embed perception status + results in the surfaces agents already hit:**
   - `GET /api/projects/{id}` clip list: include `visual_description`,
     `analysis: {status, updated_at}`, and per-kind signal status — so an agent
     listing clips immediately sees what exists and what's missing.
   - `GET /api/clips/{id}`: include the parsed canonical analysis result
     (summary/segments/highlights/suggested_clips) and compact signal results
     inline, plus `available_tools` hints (contact-sheet / frames / describe /
     search URLs).
   - `GET /api/projects/{id}/transcript`: optionally interleave visual beats
     (the "perception document" of roadmap 1.1) via `?perception=1`.
2. **A coverage endpoint:** `GET /api/projects/{id}/perception` →
   `{clips: [{clip_id, analysis: done|missing|stale, quality, camera_motion,
   embedding, detection}], totals}` — one call answers "what's backfilled?"
   (this doc's §1 tables, live).
3. **Finish the OpenAPI backfill** (`web/src/lib/openapi/existing.ts` lists the
   routes still missing) and add a short "perception tools" section to the doc
   description so `GET /api/docs` is a sufficient orientation for an agent.

### 3.4 Auto-backfill + unstick

1. **Run on ingest, not just align:** after upload, kick the deterministic
   signals (quality, motion, embed, detect — none need a transcript) and an
   audio-only unified analysis; after align, re-run the unified analysis with
   the transcript supplied (supersedes the audio-only one).
2. **Sweeper (Modal cron, e.g. every 30 min):**
   - `analyzing`/`processing` rows older than a timeout (LLM: 30 min; signals:
     15 min) → mark `error` with `timeout` reason.
   - `error` rows with `attempts < 3` → respawn with backoff.
   - Clips with media but **no** done canonical analysis / signal of each kind
     → enqueue (rate-limited) — this is the one-time backfill *and* the ongoing
     guarantee, in the same mechanism.
   - Requires an `attempts` counter on `visual_analyses` + `clip_signals`.
3. **Manual lever:** `POST /api/projects/{id}/backfill` (kinds?, force?) for
   on-demand sweeps, and to run the initial library-wide backfill under
   supervision (cost control: ~50 real clips × 1 Gemini pass + 4 cheap signals).

---

## 4. Suggested sequencing

1. Ship the unified `analyze` variant + fold-ins; flip API default; deprecate old variants.
2. Sweeper + attempts counter (unsticks the 17 stuck rows immediately; drives backfill).
3. Discoverability: coverage endpoint + inline results in clip/project GETs + OpenAPI backfill.
4. Library-wide backfill via the sweeper; verify with the coverage endpoint.

## 5. Open questions

- Chunking threshold + merge strategy for long videos (max Gemini upload is the
  hard limit; quality degrades well before it).
- Should `suggested_clips` run on every clip or only on request? (Small cost,
  but noisy on B-roll.)
- Retention of historical variant rows: keep for eval provenance, or archive?
- Detection (T5) is labeled a "test bed" — promote to auto-backfilled, or keep
  on-demand until it has a consumer?
- Cost ceiling for the initial backfill and the per-upload auto-run (flash
  pricing × high media resolution × 3 fps adds up on long clips).
