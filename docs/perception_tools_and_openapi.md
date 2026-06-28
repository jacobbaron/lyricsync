# Scope: Interactive perception tools (roadmap §1.3) + OpenAPI documentation

Status: **planning / scoped, not built.** This document specs the next perception
piece — on-demand frame & sub-range inspection — and the OpenAPI surface that makes
the whole REST API discoverable to LLM agents.

## Why

Today all visual perception is **precomputed over the whole clip**. `analyze_visuals`
uploads the entire clip to Gemini and returns coarse, sparsely-sampled highlights.
On a long clip (e.g. a 34-min vocal-tracking session) those beats are approximate —
Gemini samples thinly across 34 minutes, so "the massage moment at ~26s" is a guess,
not a measurement.

When an editing agent is *unsure about a specific region*, it currently has no
cheap way to look closer. It improvises: render a short contact-sheet clip → fetch a
signed URL → download the mp4 → extract stills with `imageio-ffmpeg` → Read the
JPEG. That works (it's how the One Car Ahead cuts were grounded) but it's slow,
burns a full render per look, and isn't exposed as a first-class capability.

§1.3 gives the agent **interactive perception**: look at frames, or ask Gemini about
a sub-range, on demand — the way a human editor scrubs.

This pairs naturally with cross-project Tier 1 (PR #77): both endpoints address a
clip by its global `clip_id`, so they work regardless of which project a clip lives
in, with no extra work.

---

## Part A — Interactive perception endpoints

Three endpoints, each backed by a small Modal function, all **API-key callable**
(`Authorization: Bearer lsk_…`) like `analyze`/`render`, and all **aggressively
cached in R2** (frames and sub-range descriptions are immutable per clip + params).

### A1. `GET /api/clips/{id}/frames`

Extract individual frames as images.

| Param | Type | Default | Meaning |
|---|---|---|---|
| `t` | float (s) | required | center/first timestamp within the clip |
| `n` | int | 1 | number of frames |
| `interval` | float (s) | 0.5 | spacing between frames |

- Returns `{ clip_id, frames: [{ t, url }] }` — short-lived signed R2 URLs.
- Backend: Modal `extract_frames` — `ffmpeg -ss <t> -frames:v 1` per frame (fast
  seek), upload to `projects/{pid}/clips/{cid}/frames/{t}.jpg`, return signed URLs.
- Cache key: `(clip_id, t, n, interval)`. Re-requests are pure cache hits.

### A2. `POST /api/clips/{id}/describe`

Ask Gemini about **just a sub-range** — the accuracy fix for long clips.

```jsonc
// body
{ "start": 24.0, "end": 30.0, "question": "what is the producer doing?" }
```

- Backend: Modal `describe_subrange` — `ffmpeg -ss <start> -to <end>` trim →
  upload the *short* clip to Gemini Flash → ask `question` (or a default
  "describe what happens, who's on screen, expressions/actions"). Because the
  upload is seconds long, sampling is dense and the read is sharp.
- Returns `{ clip_id, start, end, answer, debug? }`. Persist a row (reuse
  `visual_analyses` with a `variant:"subrange"` + a `range` field, or a new
  `clip_inspections` table) so repeats are cached and the work is auditable.
- Cache key: `(clip_id, start, end, normalized question)`.

### A3. `GET /api/clips/{id}/contact-sheet`

One tiled thumbnail image instead of N separate frames — ideal for a single
multimodal LLM read.

| Param | Type | Default | Meaning |
|---|---|---|---|
| `start` | float (s) | 0 | range start |
| `end` | float (s) | clip end | range end |
| `cols`,`rows` | int | 4×4 | grid size (→ 16 thumbs) |

- Backend: Modal `contact_sheet` — sample `cols*rows` frames across `[start,end]`,
  burn the timestamp into each tile (`drawtext`), tile with ffmpeg `tile` filter →
  one JPEG → signed URL.
- Cache key: `(clip_id, start, end, cols, rows)`.

### Exposing them as LLM tools (the point)

The value isn't the endpoints — it's wiring them into the agent loops as **tools**:

1. **In-product `generate_stories`** (already multi-turn on the Anthropic SDK): add
   `look_at_frames`, `describe_range`, `contact_sheet` tool definitions so the model
   can call them mid-generation when unsure — closes the "trust the precomputed
   highlights" gap.
2. **`reel-editor` subagent**: replace the manual render→download→extract dance in
   `.claude/agents/reel-editor.md` with these endpoints (and document them in the
   CLAUDE.md playbook so the workflow is "call `describe` on the candidate region",
   not "render a contact sheet").

### Effort

Endpoints + Modal functions + caching: ~2–3 days (ffmpeg + Gemini paths already
exist in `_analyze_worker`; this is trimming + tiling + a thinner Gemini call).
Tool-wiring into the two agent loops: ~1–2 days. Total ~1 week.

---

## Part B — OpenAPI documentation (discoverability)

### The gap

There is **no machine-readable spec** for the REST surface. Operating agents
discover capability only by reading prose (`CLAUDE.md` playbook,
`.claude/agents/reel-editor.md`) that a human keeps current. In-product tools are
discovered via hand-written tool schemas. Nothing is the single source of truth, and
nothing is introspectable.

### Goal

Ship an **OpenAPI 3.1 spec** covering the whole `web/src/app/api/**` surface
(projects, clips, transcript, visual, analyze, stories, render, edit, signed-url,
and the new §1.3 endpoints), served from the app and usable directly by agents.

### Approach (recommended: schema-first, single source of truth)

1. Define request/response shapes as **Zod schemas** colocated with each route
   handler (many already validate inputs ad hoc; formalize them).
2. Generate the OpenAPI document from those Zod schemas with
   **`@asteasolutions/zod-to-openapi`** (or `next-swagger-doc` if we prefer JSDoc
   annotations). One registry module imports every route's schemas and emits the
   document — so the spec can't drift from the code.
3. Serve it: `GET /api/openapi.json` (the document) + an optional Swagger UI at
   `/api/docs` for humans.
4. **Make §1.3 land in the spec from day one** — the three new endpoints are
   authored Zod-first so they're documented the moment they ship. This is the
   "ensure we get OpenAPI documentation" requirement: new endpoints are not
   considered done until they're in the spec.

### Why this also helps the LLM

- An agent can fetch `/api/openapi.json` and **discover every endpoint + its
  schema** instead of relying on the prose playbook — the real discoverability
  upgrade we want.
- Tool definitions for `generate_stories` / `reel-editor` can be **generated from**
  the OpenAPI operations, keeping the agent's tools and the live API in lockstep.

### Effort

Foundation (registry + generator + serve route + Swagger UI): ~2–3 days. Then an
incremental tax: each existing route gets Zod schemas (~0.5 day each, parallelizable;
or backfill opportunistically). New endpoints (§1.3) are Zod-first so they cost
nothing extra.

---

## Suggested sequencing

1. **OpenAPI foundation first** (Part B steps 1–3) — establishes the pattern and the
   discoverability surface.
2. **§1.3 endpoints, authored Zod-first** (Part A) — they land in the spec
   automatically and immediately improve perception accuracy on long clips.
3. **Wire both into the agent loops** — generate tool defs from the OpenAPI ops;
   update the CLAUDE.md playbook + reel-editor agent to call `describe`/`frames`
   instead of the manual extraction workflow.
4. Backfill Zod schemas for the remaining legacy routes as they're touched.

## Out of scope (tracked elsewhere)

- Cross-project **search/discovery** over the whole library (the deferred Phase 2
  of cross-project editing) — see `docs/cross_project_editing.md`.
- Face detection / auto-reframe (roadmap §1.2 / §2.3).
