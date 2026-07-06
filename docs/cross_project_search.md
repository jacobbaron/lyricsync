# Cross-project library search (discovery)

Find clips across **all** projects from a verbal description, without naming the
projects — so a library-wide cut ("a montage of every clip where someone
laughs") becomes possible. This is the discovery layer on top of cross-project
**editing** (`docs/cross_project_editing.md`, #77/#78): editing already lets a
timeline reference any clip by `clip_id`; search is how you *get* those
`clip_id`s.

Tracked in the `[SEARCH]` epic (#128). **S1 (#83)** shipped the keyword-search
MVP (naive in-memory scan). Four more tickets — **S2 (#119)** Postgres FTS,
**S3 (#120)** semantic search, **S6 (#123)** result enrichment, and **S7
(#124)** filters/facets — were built in parallel against that MVP and are
**consolidated into one route** here (branch
`search-consolidated-s2-s3-s6-s7`); this note describes the result, not each
ticket's original standalone diff.

## Surface

`GET /api/search?q=<text>&limit=20&mode=keyword|semantic&thumbnails=1&project=…&kind=…&min_duration=…&max_duration=…&since=…&until=…`
— API-key callable (`Authorization: Bearer lsk_…`) or browser session. Not
scoped to a project; it searches everything the caller owns (RLS-enforced).

Query params:
- `q` — the search text (required).
- `limit` — max hits (default 20, capped 50).
- `mode` — `keyword` (default) or `semantic` (S3, #120 — see below). Any other
  value is treated as `keyword`.
- `pooled` — semantic mode only: `1` to also match whole-clip pooled vectors
  (default: frames only).
- `thumbnails` — `1` to include a signed `thumbnail_url` per result (S6,
  opt-in — see "Result enrichment" below).
- `project` — restrict to one/several projects (S7); repeatable
  (`?project=a&project=b`) or comma-separated (`?project=a,b`). Each token
  matches a project id (uuid) OR project name (case-insensitive). Omit for all
  of the caller's projects. Unresolvable tokens simply match nothing (not an
  error).
- `kind` — `speech` | `visual` | `both` (default `both`, S7). `speech`
  restricts to transcript matches; `visual` restricts to
  visual_description/highlight matches. In semantic mode, embedding hits count
  as `visual` (they're all image-side vectors) — `kind=speech` short-circuits
  to an empty result without an embed call.
- `min_duration` / `max_duration` — clip length bounds in seconds
  (`clips.duration_secs`, S7). Clips with unknown duration are excluded when
  either is set.
- `since` / `until` — ISO date/timestamp bounds on `clips.recorded_at`
  (falling back to `clips.created_at` when null, S7). Clips with neither are
  excluded when either is set.

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
      "thumbnail_url": "https://...signed... (only when ?thumbnails=1)",
      "score": 21
    }
  ],
  "facets": {
    "by_project": { "Studio day 2": 2 },
    "by_kind": { "highlight": 2 }
  }
}
```

Each hit is **one clip** (its best-matching moment). `clip_id` + `timestamp`
(clip-local seconds) drop straight into a cross-project timeline item
(`clip_id`, `src_start`/`src_end`) or an overlay `in`/`out` — no extra lookup.
`duration` (`clips.duration_secs`) lets a caller validate/clamp a proposed
`src_end` without a follow-up lookup. `facets` is counted over the full
filtered candidate set, **before** slicing to `limit`, so it reflects "what's
out there" even when only a page of it is returned.

## What's indexed — keyword mode (S1 MVP → S2 FTS)

The same three sources as the S1 MVP, now indexed instead of scanned:

| Source | Where | `kind` | Timestamp |
|---|---|---|---|
| Transcript words | `clip_search_docs` — ~16-word windows per clip, materialized from `projects/{id}/merged.json` in R2 | `transcript` | anchor word's `local_start` for that window |
| Clip visual summary | `clips.visual_description_tsv` (generated `tsvector` column) | `visual_description` | `null` (whole clip) |
| Timestamped visual beats | newest `done` `visual_analyses.result.highlights[]`, unnested per-highlight | `highlight` | the highlight's `time` |

`clips.visual_description` and `visual_analyses` highlights index themselves —
generated `tsvector` columns recompute on every `UPDATE` (and backfill
automatically the moment the column is added), no application hook needed.
Transcript text is the one source that lives outside Postgres (R2), so it
needs an explicit materialized table (`clip_search_docs`) kept fresh by:
- `POST /api/projects/[id]/merge`, which windows and (re)writes a clip's docs
  right after (re)building `merged.json`, and
- `web/scripts/backfill-search-docs.ts`, a one-off/re-runnable backfill for
  clips merged *before* this indexing existed (see "Backfill status" below).

## Ranking — keyword mode

`search_library_fts(p_query, p_limit)` (SQL function, `SECURITY INVOKER`, see
`supabase/migrations/20260705231100_add_clip_search_docs.sql`) unions
`ts_rank_cd`-scored hits from all three sources against
`websearch_to_tsquery('english', q)` — a strict superset of the MVP's
bag-of-terms matching (quoted phrases, `-exclusion`, `or`). `/api/search`
calls it once per request, groups results down to the best-scoring row per
clip (mirroring the MVP's "one hit per clip" behavior), applies the `kind`/
`project`/duration/date filters (see "Filters" below), and returns the top
`limit`.

**Snippet highlighting**: `search_library_fts()` returns `snippet` as Postgres
`ts_headline(..., 'StartSel=**, StopSel=**')` over the raw source text, so
matched terms already arrive `**bold-marked**` — including stemmed matches a
naive regex over literal query terms would miss (e.g. a query for "insulate"
bolds "insulation"). This *is* S6 (#123)'s originally-proposed enrichment,
just implemented natively in the ranking function instead of as a JS-side
regex pass: S6 shipped its marker-based JS highlighting as an explicit
stand-in "until S2's FTS index lands," and now that it has, the native
`ts_headline` implementation supersedes it — there is no separate JS
highlighting step left in `route.ts` for keyword mode.

## Semantic mode (S3, #120)

`?mode=semantic` matches by **meaning** instead of keyword overlap: it embeds
`q` via the same Modal `embed_text` endpoint / CLIP model the intra-project
`GET /api/projects/[id]/search` route uses (`docs/embeddings_search.md`), then
cosine-searches `clip_embeddings` through `search_clip_embeddings_global` (see
`supabase/migrations/20260705230727_add_search_clip_embeddings_global.sql`) —
the cross-project generalization of `search_clip_embeddings` (same query
shape, minus the `project_id` filter; scoped instead by the caller's RLS via
`SECURITY INVOKER`, mirroring how keyword mode scopes `clips`/`projects`).

Hits are mapped into the same response shape as keyword mode: `kind:
"embedding"`, `timestamp` is the matched frame's `t` (`null` for a pooled
hit), and `score` is raw cosine similarity in `[0, 1]` — **not** on the same
scale as the keyword-mode score; the two modes aren't fused yet (that's hybrid
ranking, S5 #122). Unlike keyword mode, semantic mode does not collapse to one
hit per clip — multiple frames of the same clip can each appear, same as the
intra-project route.

Semantic mode gets the same S6/S7 treatment as keyword mode: `duration` and
opt-in `thumbnail_url`, and `project`/`min_duration`/`max_duration`/`since`/
`until` filters (applied post-hoc against the same clip metadata lookup
keyword mode uses — cheap, no extra round-trip). `kind=speech` short-circuits
to an empty result before the embed call even happens, since every embedding
hit is implicitly visual.

**Known limitation:** recall depends entirely on embedding coverage. Most
clips in the library are not yet embedded — auto-embed/backfill is **S4
(#121)**, not done — so semantic mode currently only surfaces clips that have
already been run through `POST /api/clips/{id}/embed`. This is expected, not a
bug.

## Result enrichment (S6, #123)

- **`duration`**: `clips.duration_secs`, added to every hit (both modes).
- **Highlighted snippets**: native `ts_headline` in keyword mode (see
  "Ranking" above); semantic-mode snippets are synthetic ("frame @ Xs
  (semantic match)" / "whole-clip semantic match") and were never
  highlighted — there's no textual match to highlight.
- **`thumbnail_url`** (optional, gated behind `?thumbnails=1`, both modes): a
  signed frame URL at the hit's `timestamp`, produced by calling the
  `/api/clips/{id}/frames` perception tool **in-process** (same
  `callPerception`/`clip_inspections` cache helpers that route uses — no extra
  HTTP hop) rather than doing an HTTP round-trip to our own API. For
  `timestamp: null` hits (whole-clip `visual_description` matches) it uses a
  representative frame at `t=0` instead of omitting the thumbnail. It's
  opt-in and only computed for the page of results actually returned (post
  `limit`) because each thumbnail is a Modal frame-extraction call — cached,
  but a cache miss is a real ffmpeg run. A per-thumbnail failure is swallowed
  (`thumbnail_url: null`) rather than failing the whole search.

## Filters and facets (S7, #124)

Filters compose with **both** ranking modes:

- `project` (id or name, repeatable/comma-separated), `min_duration`/
  `max_duration`, `since`/`until` are clip-level facts, applied identically in
  both modes against a single up-front `clips`/`projects` fetch shared by both
  code paths (avoids a second round-trip per mode).
- `kind` gates which *source* a hit may come from rather than excluding a
  clip outright: in keyword mode it filters which `search_library_fts()` rows
  are eligible before grouping to "best row per clip"; in semantic mode it's
  binary (`speech` → nothing, `visual`/`both` → normal semantic search).

Both modes originally generated their own in-memory candidate list (S1's naive
scan); S7's filter pass was written against that shape. Consolidating S2's FTS
RPC and S3's embedding RPC meant adapting the *same* filter pass
(`clipPassesFilters` in `route.ts`) to run against each RPC's grouped output
instead — the filter logic itself (project/duration/date predicates) is
unchanged from S7's original design, just re-pointed at RPC rows carrying a
`clip_id` rather than at S1's raw clip objects.

`facets` (`by_project`, `by_kind`) are tallied over the full filtered
candidate set for the request's mode, **before** slicing to `limit` — no
extra DB round-trips, just a tally over data already fetched. Note: keyword
mode facets count **clips** (one row per clip, post-dedup) while semantic mode
facets count **hits** (a clip can contribute multiple embedding rows, one per
matched frame) — by design, per "Semantic mode" above — so facet totals aren't
directly comparable across modes.

## Backfill status (transcript FTS docs)

`clip_search_docs` needs to be populated for clips merged before S2 shipped —
new merges are covered automatically via the `POST /api/projects/[id]/merge`
hook, but existing transcripts need the one-off
`web/scripts/backfill-search-docs.ts` run. See the PR description / repo
history for whether this ran against prod as part of this consolidation
(it requires `SUPABASE_SERVICE_ROLE_KEY` + R2 credentials, which aren't always
present in every environment that might ship this branch) — if it hasn't run
yet, keyword search over **transcripts** specifically will return no hits
until it does; `visual_description` and `highlight` hits are unaffected
(their generated columns backfill themselves automatically when the S2
migration's `ALTER TABLE ADD COLUMN` runs).

## Deferred (later `[SEARCH]` tickets)

- **Embedding coverage/backfill** so semantic mode has near-complete recall —
  **S4 (#121)**.
- **Hybrid ranking** fusing keyword + semantic — **S5 (#122)**.
- **Editor search UI** — **S8 (#125)**; **OpenAPI + agent tool** — **S9
  (#126)**, already documenting this consolidated shape (`SearchQuery`/
  `SearchResponse` in `web/src/lib/openapi/existing.ts`); **eval harness** —
  **S10 (#127)**.

The response shape is forward-compatible: `S5` will add a fused ranking
channel without changing the existing fields; further tickets may add facet
dimensions or result fields without breaking `{clip_id, project, project_id,
filename, kind, timestamp, snippet, score}`.
