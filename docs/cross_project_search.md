# Cross-project library search (discovery)

Find clips across **all** projects from a verbal description, without naming the
projects — so a library-wide cut ("a montage of every clip where someone
laughs") becomes possible. This is the discovery layer on top of cross-project
**editing** (`docs/cross_project_editing.md`, #77/#78): editing already lets a
timeline reference any clip by `clip_id`; search is how you *get* those
`clip_id`s.

Tracked in the `[SEARCH]` epic (#128). This note describes **S1 (#83)** (the
keyword-search MVP) and **S2 (#119)** (the Postgres FTS index that replaced
S1's naive scan), and what the later tickets add.

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

## What's indexed

The same three sources as the S1 MVP — no new content, just a different
(and now indexed) representation of it:

| Source | Where | `kind` | Timestamp |
|---|---|---|---|
| Transcript words | `projects/{id}/merged.json` in R2 (per `source` filename → clip), materialized into `clip_search_docs` windows | `transcript` | anchor word's `local_start` for that window |
| Clip visual summary | `clips.visual_description` (generated `tsvector` column) | `visual_description` | `null` (whole clip) |
| Timestamped visual beats | newest `done` `visual_analyses.result.highlights[]` (generated `tsvector` column, unnested per-highlight) | `highlight` | the highlight's `time` |

## Ranking (S2 — Postgres full-text search)

**S2 (#119)** replaced the MVP's in-memory naive scan with a Postgres FTS
index. `q` goes through `websearch_to_tsquery('english', q)` (handles quoted
phrases, `-exclusion`, `or` — a strict superset of the MVP's bag-of-terms
matching) and ranking is `ts_rank_cd` over a `tsvector` per source, via one
SQL function, `search_library_fts(p_query, p_limit)` (see
`supabase/migrations/20260705231100_add_clip_search_docs.sql`), which
`/api/search` calls instead of scanning:

| Source | Indexed as | Kept fresh |
|---|---|---|
| Transcript words | `clip_search_docs` — one row per ~16-word window per clip (`kind='transcript'`), generated `tsvector` + GIN | Transcript text lives in R2, not Postgres, so this is the one source that needs a materialized table populated by app code. `POST /api/projects/[id]/merge` refreshes it for the merged clips right after writing `merged.json`; `web/scripts/backfill-search-docs.ts` does the one-off backfill for clips merged before S2 shipped. |
| `clips.visual_description` | `clips.visual_description_tsv` — generated `tsvector` column + GIN | Automatic — Postgres recomputes the generated column on every `UPDATE` to `visual_description` (and backfills all existing rows the moment the column is added), so no application hook is needed. |
| `visual_analyses` highlights | `visual_analyses.highlights_tsv` — generated `tsvector` column (via the immutable `visual_analyses_highlights_text()` helper that flattens `result->highlights[].description`) + GIN | Same as above — automatic on `UPDATE result`, no hook needed. |

Why a table for transcripts but generated columns for the other two: the
first two sources already live in Postgres columns that change via plain
`UPDATE`s (from the merge route and from Modal's analysis worker
respectively), so a `GENERATED ALWAYS AS (...) STORED` column keeps itself
current for free. Transcript text doesn't have a Postgres column to hang a
generated expression off of — it's only ever written to R2 — so it needs its
own table with an explicit populate/refresh step. Using one unified doc table
for all three would have meant giving up that "free" auto-refresh property
for the two sources that don't need R2 access at all.

Per-clip windowing (rather than one row per clip) preserves the MVP's
"snippet centered on the match, real timestamp" behavior: `ts_rank_cd`
naturally ranks *which window* of a long transcript matched, and the
window's anchor word gives the timestamp — no separate JS-side word-position
search needed at query time. Highlights are unnested per-highlight in the SQL
(not aggregated per analysis row), so the returned timestamp/snippet is the
one matching highlight, not the whole row.

`/api/search`'s response shape and query params are unchanged from S1 — this
was a ranking/indexing swap, not a contract change. `search_library_fts` is a
plain SQL function (`SECURITY INVOKER`, the default), so it inherits the
caller's RLS automatically, same as `search_clip_embeddings`.

**Verification note:** the S2 migration (and the generated-column backfill it
triggers on `ALTER TABLE ADD COLUMN`) only takes effect once merged to `main`
and applied by the **DB migrate** CI — a PR can't live-test the FTS path
against prod before merge. See the S2 PR description for what was verified
pre-merge (a local Postgres smoke test of the migration + RPC + RLS,
`lint`/`tsc`/`build`) versus what still needs a post-merge check (`GET
/api/search` against prod).

## Deferred (later `[SEARCH]` tickets)

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
