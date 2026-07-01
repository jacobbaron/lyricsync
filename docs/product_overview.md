# LyricSync — Product Overview

*An AI editor that turns raw phone footage into finished, music-synced short-form video.*

---

## The problem

Creators (musicians, artists, brands, everyday users) shoot far more footage
than they ever turn into content. Turning a folder of unlabeled clips into a
polished, on-beat, captioned short-form video today means: manually scrubbing
hours of footage, hand-syncing cuts to a song, writing captions by hand, and
round-tripping through Premiere/CapCut/DaVinci — a skilled, slow, manual
process that doesn't scale with how much raw footage people actually capture.

LyricSync automates that pipeline end-to-end: upload clips, and an AI agent
that can *see* and *hear* the footage plans the cut, syncs it to music, and
renders it — with a real timeline underneath so a human (or the agent itself)
can keep refining it.

## What it is

LyricSync is an **API-first, agent-driven video editing platform**. Every
capability — ingest, perception, editing, rendering — is a documented REST
endpoint (`/api/openapi.json`), so the same product works two ways:

1. **As an app**: upload footage, get AI-generated rough cuts, review and
   tweak them in the browser.
2. **As a platform for AI agents**: an LLM (e.g. Claude) is handed API keys
   and the editorial workflow, and drives the entire edit — watching footage,
   making cuts, checking its own work, and shipping a finished video — with no
   human in the loop required.

Data model: **projects → clips → stories.** A project holds a library of
source footage; each generated or hand-built edit is a **story** with its own
editable timeline and rendered output.

---

## Core features

### 1. Ingest & perception — understanding raw footage automatically
- **Transcription & forced alignment** — every clip's speech is transcribed
  and word-aligned (the product's original capability: sync lyrics or
  dialogue to picture with word-level precision).
- **Visual analysis (Gemini)** — per-clip summaries and timestamped
  "highlight" beats (expression, tone, gesture — e.g. *"195s: soft affectionate
  smile as he's called perfect"*), auto-run on ingest and interleaved with the
  transcript into a single perception document the editing AI reads.
- **On-demand inspection tools** — contact sheets (tiled thumbnails with
  burned-in timestamps), targeted frame extraction, and sub-clip Q&A
  (*"is the subject looking at the camera at 12s?"*) so the AI (or a human)
  can zoom in on any moment instead of trusting a coarse summary.
- **Technical quality & motion signals** — automated sharpness/exposure/shake
  and camera-motion detection (static/pan/tilt/zoom/handheld) per clip, so
  unusable takes are flagged before they're cut in.
- **Cross-clip semantic search** — CLIP-embedding search across an entire
  footage library ("find the shot with the dog on the beach") without
  watching anything.

### 2. AI-generated rough cuts
- One request (`POST /api/projects/{id}/generate`) produces multiple
  candidate story edits from raw footage — Claude reads the perception
  documents across all clips and proposes cuts, respecting line/sentence
  boundaries and picking footage that actually matches what's being said.
- Structured **generation rounds** persist every prompt/response for
  reproducibility and future quality evaluation.

### 3. A real timeline underneath every edit
- Edits aren't a one-shot render — they're a versioned **Edit Decision List**
  (`timeline_json`) with video, text, and audio tracks, editable through a
  typed operations API: trim, reorder, delete, split, retime (0.25–20×
  slow-mo/fast/time-lapse), mute, crossfades/transitions, title-card overlays,
  audio effects (echo/reverb/cavern).
- **Filler/silence cleanup** — a dry-run-first "clean speech" op previews
  exactly what filler and dead air would be trimmed, and how much runtime
  it saves, before committing.
- **Automatic canvas fitting** — output frame auto-fits to source aspect
  (3:4, 4:3, landscape) or can be explicitly pinned (e.g. 1080×1920).
- **Cross-project assembly** — one output can splice footage from a user's
  *entire* library, not just a single upload batch.
- Every change is a durable, recoverable DB row — nothing is a throwaway
  render.

### 4. Music & sync
- **Song alignment** — durable clip↔song time alignment (chroma-DTW), so any
  footage window can be matched to the point in a track it was filmed to.
- **Music beds** — attach a finished song under a whole cut, independent of
  the footage's own audio.
- **Lip-sync anchoring** — pin a "hero" clip's on-camera moment to the exact
  point in the music bed it was performed to, while the bed keeps playing
  underneath the rest of the cut.

### 5. Rendering & delivery
- Cloud rendering on Modal (GPU-capable, autoscaling, no user-managed
  infrastructure); output stored in Cloudflare R2 and delivered via signed,
  time-limited URLs — no login wall for the person receiving a video.
- Full render history and revisions per story.

### 6. Agent-native platform
- Full OpenAPI spec, API-key auth, and a workflow designed for an LLM to
  operate directly — this doc set is itself written for an AI agent to pick
  up and run a professional edit end-to-end (perception → cut → verify →
  deliver), which is also exactly the workflow a human editor follows using
  the same endpoints.

---

## What's next (in active development)

- **Self-evaluation loop** — after every render, an AI critique (hook
  strength, pacing, cut smoothness, caption legibility) feeds back into the
  next edit automatically, turning generation into a closed-loop, scored
  iteration rather than a single shot.
- **Face tracking → smart auto-reframe** — know who's on screen and when, to
  auto-punch-in on reactions and keep speakers centered in vertical crops.
- **Styled, word-synced captions** — karaoke-style and branded caption
  presets driven by the exact word timings already captured at ingest.
- **Draft-quality fast previews + parallel/cached rendering** — sub-second
  iteration instead of waiting on full-quality encodes for every tweak.

---

## Technology

- **Frontend/API**: Next.js on Vercel
- **Background compute**: Modal (Python) — transcription, forced alignment,
  visual/audio analysis, rendering
- **AI models**: Claude (editorial reasoning & agentic tool use), Gemini
  (visual understanding), WhisperX (forced alignment), CLIP (semantic
  embeddings)
- **Database**: Supabase (Postgres + pgvector)
- **Storage**: Cloudflare R2

## Origin

LyricSync began as a focused CLI tool for one hard problem — forced-alignment
captioning of music videos against a fixed lyric transcript — and has grown
into a full agentic editing platform built on that same alignment core, now
generalized to any footage, any voice, and any music.

---

*This document describes shipped product capability as of the current
codebase. It intentionally omits market sizing, pricing, and financial
projections — those are business inputs, not something to infer from the
code, and should be supplied separately for an investor-facing deck.*
