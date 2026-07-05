# Cross-project library search (discovery)

Find clips across **all** projects from a verbal description, without naming the
projects — so a library-wide cut ("a montage of every clip where someone
laughs") becomes possible. This is the discovery layer on top of cross-project
**editing** (`docs/cross_project_editing.md`, #77/#78): editing already lets a
timeline reference any clip by `clip_id`; search is how you *get* those
`clip_id`s.

Tracked in the `[SEARCH]` epic (#128). This note describes **S1 (#83)** — the
keyword-search MVP — and what the later tickets add.

## Surface

`GET /api/search?q=<text>&limit=20` — API-key callable (`Authorization: Bearer
lsk_…`) or browser session. Not scoped to a project; it searches everything the
caller owns (RLS-enforced).

Query params:
- `q` — the search text (required).
- `limit` — max hits (default 20, capped 50).

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
      "kind": "highlight",
      "timestamp": 195.0,
      "snippet": "soft affectionate laugh as he's called perfect",
      "score": 21
    }
  ]
}
```

Each hit is **one clip** (its best-matching moment). `clip_id` + `timestamp`
(clip-local seconds) drop straight into a cross-project timeline item
(`clip_id`, `src_start`/`src_end`) or an overlay `in`/`out` — no extra lookup.

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
- **Richer results** (`ts_headline` snippets, thumbnail URLs) — **S6 (#123)**;
  **filters/facets** (project, kind, duration, date) — **S7 (#124)**.
- **Editor search UI** — **S8 (#125)**; **OpenAPI + agent tool** — **S9 (#126)**;
  **eval harness** — **S10 (#127)**.

The MVP endpoint's response shape is forward-compatible: the later tickets add
fields (thumbnail, per-source `sources[]`, facet counts) and ranking channels
without changing `{clip_id, project, timestamp, snippet}`.
