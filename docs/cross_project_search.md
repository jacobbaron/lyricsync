# Cross-project library search (discovery)

Find clips across **all** projects from a verbal description, without naming the
projects — so a library-wide cut ("a montage of every clip where someone
laughs") becomes possible. This is the discovery layer on top of cross-project
**editing** (`docs/cross_project_editing.md`, #77/#78): editing already lets a
timeline reference any clip by `clip_id`; search is how you *get* those
`clip_id`s.

Tracked in the `[SEARCH]` epic (#128). **S1 (#83)** shipped the keyword-search
MVP (naive in-memory scan). Five more tickets — **S2 (#119)** Postgres FTS,
**S3 (#120)** semantic search, **S6 (#123)** result enrichment, **S7 (#124)**
filters/facets, and **S5 (#122)** hybrid RRF ranking — were built against that
MVP and are **consolidated into one route** here
(`web/src/app/api/search/route.ts`); this note describes the result, not each
ticket's original standalone diff.

⚠️ **Behavior change (S5 #122):** the default `mode` changed from `keyword` to
`hybrid`. A caller that never passed `mode` used to get pure keyword-ranked
results; it now gets hybrid-fused results (see below). Pass `mode=keyword`
explicitly to keep the exact old behavior.

## Surface

`GET /api/search?q=<text>&limit=20&mode=hybrid|keyword|semantic&thumbnails=1&project=…&kind=…&min_duration=…&max_duration=…&since=…&until=…`
— API-key callable (`Authorization: Bearer lsk_…`) or browser session. Not
scoped to a project; it searches everything the caller owns (RLS-enforced).

Query params:
- `q` — the search text (required).
- `limit` — max hits (default 20, capped 50).
- `mode` — `hybrid` (**default**, S5 #122), `keyword` (S2, #119), or
  `semantic` (S3, #120 — see below). Any other/unrecognized value — including
  no `mode` param at all — is treated as `hybrid`.
- `pooled` — semantic channel only (applies to `mode=semantic` and hybrid's
  semantic channel): `1` to also match whole-clip pooled vectors (default:
  frames only).
- `thumbnails` — `1` to include a signed `thumbnail_url` per result (S6,
  opt-in — see "Result enrichment" below).
- `project` — restrict to one/several projects (S7); repeatable
  (`?project=a&project=b`) or comma-separated (`?project=a,b`). Each token
  matches a project id (uuid) OR project name (case-insensitive). Omit for all
  of the caller's projects. Unresolvable tokens simply match nothing (not an
  error).
- `kind` — `speech` | `visual` | `both` (default `both`, S7). `speech`
  restricts to transcript matches; `visual` restricts to
  visual_description/highlight matches. In semantic mode (and hybrid's
  semantic channel), embedding hits count as `visual` (they're all image-side
  vectors) — `kind=speech` short-circuits that channel to an empty result
  without an embed call.
- `min_duration` / `max_duration` — clip length bounds in seconds
  (`clips.duration_secs`, S7). Clips with unknown duration are excluded when
  either is set.
- `since` / `until` — ISO date/timestamp bounds on `clips.recorded_at`
  (falling back to `clips.created_at` when null, S7). Clips with neither are
  excluded when either is set.

Response (hybrid, the default):

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
      "score": 0.0317,
      "sources": ["keyword", "semantic"]
    }
  ],
  "facets": {
    "by_project": { "Studio day 2": 2 },
    "by_kind": { "highlight": 2 }
  },
  "mode": "hybrid"
}
```

Each hit is **one clip** (its best-matching moment) — in `mode=semantic`
alone, multiple frame hits per clip are still allowed (see "Semantic mode"
below); every other mode, including hybrid, is one-hit-per-clip. `clip_id` +
`timestamp` (clip-local seconds) drop straight into a cross-project timeline
item (`clip_id`, `src_start`/`src_end`) or an overlay `in`/`out` — no extra
lookup. `duration` (`clips.duration_secs`) lets a caller validate/clamp a
proposed `src_end` without a follow-up lookup. `sources` (S5 #122) says which
channel(s) produced the hit — `["keyword", "semantic"]` when a clip matched
both, a single-element array otherwise. `facets` is counted over the full
filtered (and, in hybrid, fused/deduped) candidate set, **before** slicing to
`limit`, so it reflects "what's out there" even when only a page of it is
returned. `mode` echoes which mode actually served the request. `warnings`
(hybrid only, omitted when everything worked) reports a degraded semantic
channel — see "Hybrid ranking" below.

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
scale as the keyword-mode score (hybrid mode, below, is what fuses the two).
Unlike keyword mode (and unlike hybrid mode), semantic mode *alone* does not
collapse to one hit per clip — multiple frames of the same clip can each
appear, same as the intra-project route.

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
bug — hybrid mode below degrades gracefully to keyword-only results for
clips (or entire libraries) that aren't embedded yet.

## Hybrid ranking (S5, #122)

`mode=hybrid` — **the default** — fuses the keyword and semantic channels
into one ranked, deduped list, so a single query surfaces the best clip
whether the match is lexical (exact/stemmed word) or conceptual (semantically
related but no shared words).

**Fusion: Reciprocal Rank Fusion (RRF), k=60.** Both channels run in parallel
(`Promise.all` — they're independent network calls: one Postgres RPC, one
Modal embed + Postgres RPC). Each channel's candidates are grouped to one
best-scoring row per `clip_id` (keyword mode already does this; semantic
mode's raw output is additionally grouped here, keeping only each clip's
best-scoring frame, in RPC order — the RPC returns rows best-similarity-first,
so the first occurrence per `clip_id` is already its best frame). Every clip
across the two ranked lists then gets a fused score:

```
score = Σ over channels the clip appears in of  1 / (60 + rank_in_that_channel)
```

(`rank` is 1-based within that channel's own list.) RRF was picked over a
weighted score blend because keyword scores (`ts_rank_cd`) and semantic scores
(cosine similarity) live on entirely different, non-comparable scales — RRF
sidesteps that by fusing on *rank* instead of raw score, needs no per-corpus
tuning, and is the standard, robust default for this exact two-channel-fusion
problem. `k=60` is the typical RRF constant (it damps the influence of any
single top rank without a query-specific tuning pass); no evidence from
testing this ticket suggested a different constant was warranted.

**Dedup key: `clip_id` alone.** The ticket's "same clip/timestamp" dedup
question doesn't have a clean answer at the timestamp level: keyword hits
carry clip-local timestamps from transcript windows / highlight beats / `null`
(whole-clip `visual_description` matches), while semantic hits carry
per-frame float timestamps from a completely different clock (frame sampling
interval) that will essentially never exactly equal a keyword hit's timestamp
for what a human would call "the same moment." Rather than build fuzzy
timestamp-overlap matching (a real project of its own, easy to get subtly
wrong), hybrid mode dedupes at the `clip_id` level — the same granularity
keyword mode (and the MVP before it) already uses for "one hit per clip." A
clip found by both channels gets `sources: ["keyword", "semantic"]`; the
display fields (`kind`/`timestamp`/`snippet`) come from **whichever channel
ranked the clip better** (lower rank number) — "the best-ranked
representative" — rather than trying to merge two different timestamps into
one.

**Failure handling: degrade, don't fail.** Before S5, keyword mode (the old
default) never depended on Modal — it's pure Postgres. Making hybrid the new
default means every default search now costs one extra network round-trip
(the embed call) and a new failure mode (Modal down / misconfigured). Rather
than let that regress the reliability of what's now the default search
endpoint, hybrid mode catches a failing semantic channel, logs it
server-side, and falls back to keyword-only ranking (RRF over a single list —
mathematically equivalent to the plain keyword ranking, since `1/(60+rank)` is
monotonic in `rank`) — the response still succeeds, with a `warnings` field
naming the degradation. A **failing keyword channel** (bad query / DB error),
by contrast, still fails the whole hybrid request — those are the same
conditions that already 400/500'd under the pre-S5 keyword-only default, so
hybrid's behavior for them is unchanged.

**Performance note:** hybrid's extra Modal round-trip (embedding the query)
is the one real cost of the new default. The file doesn't have infrastructure
for canceling one channel early if the other returns faster, and both calls
are already independent, so a simple `Promise.all` is the whole
"parallelization" story here — no further engineering was justified for this
ticket. In informal testing this added on the order of a few hundred
milliseconds versus keyword-only, dominated by the Modal cold-start/embed
call; see the PR for concrete numbers.

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

Filters compose with **all three** ranking modes:

- `project` (id or name, repeatable/comma-separated), `min_duration`/
  `max_duration`, `since`/`until` are clip-level facts, applied identically in
  every mode against a single up-front `clips`/`projects` fetch shared by all
  code paths (avoids a second round-trip per mode). Hybrid applies them
  per-channel (before fusion), same as if you'd run each channel solo.
- `kind` gates which *source* a hit may come from rather than excluding a
  clip outright: in keyword mode (and hybrid's keyword channel) it filters
  which `search_library_fts()` rows are eligible before grouping to "best row
  per clip"; in semantic mode (and hybrid's semantic channel) it's binary
  (`speech` → nothing, `visual`/`both` → normal semantic search).

Keyword and semantic modes originally generated their own in-memory candidate
list (S1's naive scan); S7's filter pass was written against that shape.
Consolidating S2's FTS RPC and S3's embedding RPC meant adapting the *same*
filter pass (`clipPassesFilters` in `route.ts`) to run against each RPC's
grouped output instead — the filter logic itself (project/duration/date
predicates) is unchanged from S7's original design, just re-pointed at RPC
rows carrying a `clip_id` rather than at S1's raw clip objects. S5's hybrid
mode reuses the exact same filter pass a third time (via the shared
`keywordCandidates()`/`semanticCandidates()` helpers each mode calls) — no
new filter logic was needed.

`facets` (`by_project`, `by_kind`) are tallied over the full filtered
candidate set for the request's mode, **before** slicing to `limit` — no
extra DB round-trips, just a tally over data already fetched. Note: keyword
mode and hybrid mode facets count **clips** (one row per clip, post-dedup —
hybrid's `by_kind` reflects each clip's best-ranked-representative channel,
so `embedding` shows up there too when a clip's semantic hit outranked its
keyword hit) while semantic mode *alone* facets count **hits** (a clip can
contribute multiple embedding rows, one per matched frame) — by design, per
"Semantic mode" above — so semantic-mode facet totals aren't directly
comparable to the other two modes'.

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

## Eval harness (S10, #127)

`web/scripts/search-eval.ts` is a small regression harness: it runs a curated
"golden set" of real verbal queries (`web/scripts/search-eval/golden-queries.json`
— built by actually exploring the live library's clips/transcripts/highlights,
not hypothetical queries) against live `GET /api/search` for all three modes
(`keyword`/`semantic`/`hybrid`), checks whether each query's expected clip(s)
land in the top-k, and reports a per-mode success@k summary plus a per-query
hit/miss table. Use it to catch a ranking regression after touching S2/S3/S5
(FTS scoring, embedding coverage, or RRF fusion) — it's meant to be re-run, not
a one-off.

**Run it** (from `web/`, needs the same `LYRICSYNC_BASE_URL` /
`LYRICSYNC_API_KEY` every agent web session already has exported, or your own
prod API key):

```bash
npm run eval:search
# or, with options:
node --experimental-strip-types scripts/search-eval.ts --k 10 \
  --report-json scripts/search-eval/last-report.json
```

**What "hit" means**: for a golden query, a mode "hits" if *any* of that
query's `expected_clip_ids` appears among the top-`k` results returned for
that mode (default `k=10`); the reported rank is the best (lowest) rank among
them. The per-mode summary (`success@k`) is the fraction of golden queries
that hit — the standard metric for a small hand-built golden set like this
one. `golden-queries.json`'s per-query `notes` call out which queries are
deliberately **keyword-favoring** (exact lexical overlap — e.g. `"dumper"`),
**semantic-favoring** (no shared words, only conceptual/visual overlap — e.g.
`"someone laughing"`, the ticket's own example), or **all-modes-agree**
sanity checks (e.g. `"cat"`), so a future ranking change that regresses one
channel shows up as a category-specific dip rather than just a single
aggregate number moving.

A live run against prod (2026-07-06, 15 golden queries, `k=10`) found:

| Mode | success@10 |
|---|---|
| keyword | 10/15 (66.7%) |
| semantic | 11/15 (73.3%) |
| hybrid | 15/15 (100.0%) |

Hybrid met or beat the best single channel on every query in this golden set —
the acceptance criterion this ticket asked the harness to check for future
ranking changes. See the S10 PR description for the full per-query table.

Manual-run only for now (not wired as a CI gate): it hits **live prod**, so
making it a blocking/required check would fail CI on transient prod issues
(Modal cold starts, embedding coverage drift) unrelated to the PR under
review. Re-run it manually after any change to S2/S3/S5 ranking code.

## Deferred (later `[SEARCH]` tickets)

- **Embedding coverage/backfill** so semantic mode (and hybrid's semantic
  channel) have near-complete recall — **S4 (#121)**, still not done as of
  S5; hybrid degrades gracefully in the meantime (see "Hybrid ranking"
  above), it just can't fuse in what isn't embedded yet.
- **Editor search UI** — **S8 (#125)**; **OpenAPI + agent tool** — **S9
  (#126)**, already documenting this consolidated shape (`SearchQuery`/
  `SearchResponse` in `web/src/lib/openapi/existing.ts`, updated for S5 to add
  the `hybrid` mode, `sources`, and `mode`/`warnings` response fields).
  **Eval harness** — **S10 (#127)** — see "Eval harness" above.

The response shape remains forward-compatible: further tickets may add facet
dimensions or result fields without breaking `{clip_id, project, project_id,
filename, kind, timestamp, snippet, score, sources}`.
