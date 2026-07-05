# Cross-project library search (discovery)

Find clips across **all** projects from a verbal description, without naming the
projects — so a library-wide cut ("a montage of every clip where someone
laughs") becomes possible. This is the discovery layer on top of cross-project
**editing** (`docs/cross_project_editing.md`, #77/#78): editing already lets a
timeline reference any clip by `clip_id`; search is how you *get* those
`clip_id`s.

Tracked in the `[SEARCH]` epic (#128). This note describes **S1 (#83)** — the
keyword-search MVP — plus **S6 (#123)**'s result enrichment, and what the
remaining tickets add.

## Surface

`GET /api/search?q=<text>&limit=20&thumbnails=1` — API-key callable
(`Authorization: Bearer lsk_…`) or browser session. Not scoped to a project; it
searches everything the caller owns (RLS-enforced).

Query params:
- `q` — the search text (required).
- `limit` — max hits (default 20, capped 50).
- `thumbnails` — `1` to include a signed `thumbnail_url` per result (S6,
  opt-in — see below).

Response:

```json
{
  "query": "someone laughing",
  "terms": ["someone", "laughing"],
  "count": 2,
  "results": [
    {
      "clip_id": "0b9c…-uuid",
      "project": "Studio day 2",
      "project_id": "1a2b…-uuid",
      "filename": "IMG_2427.mov",
      "duration": 42.3,
      "kind": "highlight",
      "timestamp": 195.0,
      "snippet": "soft affectionate **laugh** as he's called perfect",
      "thumbnail_url": "https://...signed...",
      "score": 21
    }
  ]
}
```

Each hit is **one clip** (its best-matching moment). `clip_id` + `timestamp`
(clip-local seconds) drop straight into a cross-project timeline item
(`clip_id`, `src_start`/`src_end`) or an overlay `in`/`out` — no extra lookup.
`duration` (`clips.duration_secs`) is included so a caller can validate/clamp a
proposed `src_end` without a follow-up lookup.

## Result enrichment (S6 — #123)

- **Highlighted snippets**: matched query terms are wrapped inline in
  `**term**` (case-insensitive, word-boundary aware — `cat` won't match inside
  `category`). This is a dependency-free stand-in for Postgres `ts_headline`;
  swap it for real `ts_headline` once **S2 (#119)**'s FTS index merges (as of
  this writing it hadn't, so `route.ts` still does its own tokenizing/scoring
  rather than querying an index).
- **`duration`**: `clips.duration_secs`, added to every hit.
- **`thumbnail_url`** (optional, gated behind `?thumbnails=1`): a signed frame
  URL at the hit's `timestamp`, produced by calling the `/api/clips/{id}/frames`
  perception tool **in-process** (same `callPerception`/`clip_inspections`
  cache helpers that route uses — no extra HTTP hop) rather than doing an HTTP
  round-trip to our own API. For `timestamp: null` hits (whole-clip
  `visual_description` matches) it uses a representative frame at `t=0` instead
  of omitting the thumbnail. It's opt-in and only computed for the page of
  results actually returned (post `limit`) because each thumbnail is a Modal
  frame-extraction call — cached, but a cache miss is a real ffmpeg run, so
  requesting it for every hit by default would meaningfully slow the endpoint.
  A per-thumbnail failure is swallowed (`thumbnail_url: null`) rather than
  failing the whole search.

## What's indexed (MVP)

All text we **already have**, no new tables or embeddings:

| Source | Where | `kind` | Timestamp |
|---|---|---|---|
| Transcript words | `projects/{id}/merged.json` in R2 (per `source` filename → clip) | `transcript` | `local_start` of the matched word |
| Clip visual summary | `clips.visual_description` | `visual_description` | `null` (whole clip) |
| Timestamped visual beats | newest `done` `visual_analyses.result.highlights[]` (`{time, description, …}`) | `highlight` | the highlight's `time` |

The route fans out: one `clips` query (RLS → all projects), one `projects`
query for names, one `visual_analyses` query (`status = 'done'`, `result` only —
never the large `debug` blob), and one R2 read of each referenced project's
`merged.json` (best-effort; a missing/unbuilt transcript is skipped). Each DB
query is paginated (`.range()` in a loop) so a library larger than PostgREST's
~1000-row response cap isn't silently truncated. Transcript
words are attributed to a clip by `(project_id, filename)`, so filename
collisions across projects can't cross-attribute.

## Ranking (MVP — deliberately naive)

Plain keyword matching, no index:

- `q` is tokenized into lowercased, punctuation-stripped **terms**.
- For each clip, every candidate text (transcript / description / each
  highlight) is scored: `10 × (distinct terms matched) + (total term
  occurrences) + 50 (if the full query appears as a phrase)`.
- The clip's best-scoring candidate becomes its hit; hits with score 0 are
  dropped. Results are sorted by score, then sliced to `limit`.
- Transcript snippets are a ±word window around the first match; description /
  highlight snippets are the matched text itself.

This is intentionally simple: correct, dependency-free, and good enough to
verify the cross-project discovery loop end-to-end. It biases toward long
transcripts (more occurrences) and does no stemming or synonymy — see Deferred.

## Deferred (later `[SEARCH]` tickets)

- **Postgres full-text index** (tsvector/GIN + `ts_rank`) over the same text, so
  ranking is principled and scans don't grow with the library — **S2 (#119)**.
- **Semantic / vector search** over CLIP embeddings, library-wide (generalize
  the intra-project `search_clip_embeddings` RPC / `docs/embeddings_search.md`) —
  **S3 (#120)**, with embedding coverage/backfill in **S4 (#121)**.
- **Hybrid ranking** fusing keyword + semantic — **S5 (#122)**.
- **Filters/facets** (project, kind, duration, date) — **S7 (#124)**.
- **Editor search UI** — **S8 (#125)**; **OpenAPI + agent tool** — **S9 (#126)**;
  **eval harness** — **S10 (#127)**.

Result enrichment (`duration`, highlighted snippets, `thumbnail_url`) shipped in
**S6 (#123)** — see "Result enrichment" above; it's the marker-based
highlighting stand-in until S2's FTS index lands, at which point swap in real
`ts_headline`.

The MVP endpoint's response shape is forward-compatible: the later tickets add
fields (per-source `sources[]`, facet counts) and ranking channels without
changing `{clip_id, project, timestamp, snippet}`.
