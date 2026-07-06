# Clip & frame embeddings + semantic search (PERCEPTION T4)

Per-frame and pooled **CLIP embeddings** stored in pgvector, enabling
cross-clip semantic search over footage (and, later, near-duplicate-take
clustering and B-roll↔narration matching).

## Model

`sentence-transformers/clip-ViT-B-32` — a **512-dim space shared by the image
and text encoders**, so a text query and a video frame land in the same space
and are directly comparable by cosine similarity. All vectors are stored
L2-normalized (cosine == dot product). The model id + dim live in the
`clip_embeddings.model` / `vector(512)` column so we can re-embed with a
different model later without guessing (`modal/embedding.py:EMBED_MODEL`,
`EMBED_DIM`).

## Data model

`clip_embeddings(id, clip_id → clips, t real null, embedding vector(512),
model text, created_at)`

- One row per sampled frame (`t` = seconds from clip start), sampled at
  ~1 fps and capped at `EMBED_MAX_FRAMES` (240).
- One **pooled** row per clip with `t = NULL` — the mean of the frame vectors,
  re-normalized — as a whole-clip vector.
- HNSW cosine index (`vector_cosine_ops`); RLS scoped to the project owner.

Re-embedding a clip is idempotent: the worker deletes the clip's prior rows for
that model before inserting.

## Endpoints (Bearer `lsk_…`)

### Index a clip
`POST /api/clips/{id}/embed` → `202 {id, kind:"embedding", status:"processing"}`.
Status is tracked as a `clip_signals` row (`kind='embedding'`) so it's pollable
like the other perception workers:
`GET /api/clips/{id}/signals?kind=embedding` → `result {n_frames, model, dim}`
when done.

### Search
`GET /api/projects/{id}/search?q=<text>&limit=10&pooled=0`
→ `{ query, results: [{ clip_id, t, score }] }`, ranked by cosine similarity
(`score` in `[0,1]`; `t = null` = a pooled clip-level hit).

- `limit` — max hits (default 10, capped 50).
- `pooled=1` — also match whole-clip pooled vectors (default: frames only).

The route embeds the query via the Modal `embed_text` endpoint (same CLIP
model), then runs the `search_clip_embeddings` RPC, which executes under the
caller's RLS — results are scoped to the project owner. The returned
`{clip_id, t}` pairs are directly usable to build a (cross-project) cut.

## Pipeline

```
POST /api/clips/{id}/embed
   → clip_signals row (kind=embedding, processing)
   → Modal embed_clip → _embed_worker:
       ffmpeg ~1fps frames → CLIP encode → per-frame + pooled rows
       → clip_embeddings ; signal → done

GET /api/projects/{id}/search?q=…
   → Modal embed_text (query → 512-d vector)
   → search_clip_embeddings RPC (pgvector cosine, RLS-scoped)
   → ranked {clip_id, t, score}
```

## Deferred

Near-duplicate/take clustering, B-roll↔narration matching, and re-embedding
with a larger model are future consumers of this table — not in this ticket.

A cross-project surface now exists: `GET /api/search?mode=semantic` (SEARCH
S3, #120, consolidated into `search-consolidated-s2-s3-s6-s7` alongside S2/S6/
S7) reuses this same table/model via `search_clip_embeddings_global`, a
project-agnostic sibling of `search_clip_embeddings` scoped by the caller's
RLS instead of a project id — see `docs/cross_project_search.md`. Coverage is
still whatever's been embedded via this route's `POST /api/clips/{id}/embed`;
exhaustive backfill is S4 (#121), not done.
