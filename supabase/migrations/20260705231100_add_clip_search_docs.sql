-- SEARCH S2 (#119): Postgres full-text search to replace the S1 (#83)
-- in-memory naive scan. See docs/cross_project_search.md.
--
-- Design: three text sources, indexed where they already live rather than
-- one big table, because two of them are simple columns and one requires an
-- out-of-Postgres fetch:
--
--   1. clips.visual_description   — plain column, already in Postgres.
--      -> generated tsvector column + GIN index (auto-refreshes on UPDATE,
--         no application hook needed).
--   2. visual_analyses highlights — jsonb inside visual_analyses.result,
--      already in Postgres.
--      -> generated tsvector column (via an immutable helper that flattens
--         highlight descriptions out of the jsonb) + GIN index (also
--         auto-refreshes on UPDATE, no hook needed).
--   3. Transcript words          — live in R2 (projects/{id}/merged.json),
--      NOT in Postgres. Postgres can't index R2 content directly, so this is
--      the one source that needs a materialized table populated by
--      application code that reads R2: clip_search_docs. Populated by
--      scripts/backfill-search-docs.ts (one-off/backfill) and refreshed
--      incrementally by POST /api/projects/[id]/merge whenever a project's
--      transcript is (re)built — see that route for the hook.
--
-- Each clip's transcript is split into small fixed-size, non-overlapping
-- word windows (one clip_search_docs row per window) rather than one row per
-- clip, so ts_rank_cd naturally ranks *which part* of a long transcript
-- matched, and the window's anchor word gives a real timestamp — mirroring
-- what the S1 MVP did by hand (±5-word snippet centered on the match).
--
-- search_library_fts() unions all three sources, already ranked with
-- ts_rank_cd over websearch_to_tsquery, and returns per-hit (clip_id, kind,
-- timestamp, snippet, score) — /api/search calls this instead of scanning.
-- It's a plain SQL function (SECURITY INVOKER, the default), so it inherits
-- the caller's RLS automatically, exactly like search_clip_embeddings.

-- ── 1. clips.visual_description ─────────────────────────────────────────────

alter table clips add column if not exists visual_description_tsv tsvector
  generated always as (to_tsvector('english', coalesce(visual_description, ''))) stored;

create index if not exists clips_visual_description_tsv_idx
  on clips using gin (visual_description_tsv);

-- ── 2. visual_analyses highlights ───────────────────────────────────────────

-- Flattens result->highlights[].description into one space-joined string, for
-- a cheap coarse "does this row have any matching highlight" GIN pre-filter.
-- Immutable (pure jsonb read, no table/clock access) so it's legal in a
-- generated column and in an index expression.
create or replace function visual_analyses_highlights_text(result jsonb)
returns text
language sql
immutable
as $$
  select coalesce(
    string_agg(h ->> 'description', ' '),
    ''
  )
  from jsonb_array_elements(coalesce(result -> 'highlights', '[]'::jsonb)) h
  where h ->> 'description' is not null;
$$;

alter table visual_analyses add column if not exists highlights_tsv tsvector
  generated always as (to_tsvector('english', visual_analyses_highlights_text(result))) stored;

create index if not exists visual_analyses_highlights_tsv_idx
  on visual_analyses using gin (highlights_tsv);

-- Newest-done-analysis-per-clip lookup (mirrors the S1 route's "newest wins"
-- rule) — supports search_library_fts()'s highlight branch.
create index if not exists visual_analyses_clip_created_idx
  on visual_analyses (clip_id, created_at desc);

-- ── 3. clip_search_docs (transcript windows, R2-sourced) ────────────────────

create table if not exists clip_search_docs (
  id          uuid primary key default gen_random_uuid(),
  clip_id     uuid not null references clips(id) on delete cascade,
  project_id  uuid not null references projects(id) on delete cascade,
  kind        text not null default 'transcript',
  window_idx  int not null default 0,
  source_text text not null default '',
  timestamp   double precision,
  doc         tsvector generated always as (to_tsvector('english', source_text)) stored,
  updated_at  timestamptz not null default now()
);

create index if not exists clip_search_docs_clip_kind_idx
  on clip_search_docs (clip_id, kind);
create index if not exists clip_search_docs_project_id_idx
  on clip_search_docs (project_id);
create index if not exists clip_search_docs_doc_gin_idx
  on clip_search_docs using gin (doc);

alter table clip_search_docs enable row level security;

-- RLS mirrors clip_signals/clip_embeddings (scope to the owner of the clip's
-- project), but clip_search_docs denormalizes project_id directly so the
-- policy doesn't need a join through clips for every row check.
-- (drop-then-create so this migration is safe to re-run, since `create
-- policy` has no `if not exists` form)
drop policy if exists clip_search_docs_owner_all on clip_search_docs;
create policy clip_search_docs_owner_all on clip_search_docs
  for all to authenticated
  using (exists (
    select 1 from projects p
    where p.id = clip_search_docs.project_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from projects p
    where p.id = clip_search_docs.project_id
      and p.owner = (auth.jwt() ->> 'email')
  ));

-- ── 4. search_library_fts(): ranked union of all three sources ──────────────
--
-- p_query goes through websearch_to_tsquery (handles quoted phrases, "-" for
-- exclusion, "or" — a strict superset of the MVP's bag-of-terms behavior).
-- Highlights are unnested per-highlight (not just per-analysis-row) so the
-- returned timestamp/snippet is the specific matching highlight, not the
-- whole row; highlights_tsv is used first as a cheap GIN-accelerated
-- per-row pre-filter before that per-highlight unnest.
create or replace function search_library_fts(p_query text, p_limit int default 200)
returns table (
  clip_id   uuid,
  kind      text,
  "timestamp" double precision,
  snippet   text,
  score     real
)
language sql
stable
as $$
  with q as (
    select websearch_to_tsquery('english', p_query) as tsq
  ),
  transcript_hits as (
    select
      csd.clip_id,
      'transcript'::text as kind,
      csd.timestamp,
      csd.source_text as snippet,
      ts_rank_cd(csd.doc, q.tsq) as score
    from clip_search_docs csd, q
    where csd.kind = 'transcript'
      and csd.doc @@ q.tsq
  ),
  description_hits as (
    select
      c.id as clip_id,
      'visual_description'::text as kind,
      null::double precision as timestamp,
      c.visual_description as snippet,
      ts_rank_cd(c.visual_description_tsv, q.tsq) as score
    from clips c, q
    where c.visual_description_tsv @@ q.tsq
  ),
  newest_analysis as (
    select distinct on (va.clip_id)
      va.clip_id, va.result, va.highlights_tsv
    from visual_analyses va
    where va.status = 'done'
    order by va.clip_id, va.created_at desc
  ),
  highlight_hits as (
    select
      na.clip_id,
      'highlight'::text as kind,
      nullif(h ->> 'time', '')::double precision as timestamp,
      h ->> 'description' as snippet,
      ts_rank_cd(to_tsvector('english', coalesce(h ->> 'description', '')), q.tsq) as score
    from newest_analysis na
    cross join lateral jsonb_array_elements(coalesce(na.result -> 'highlights', '[]'::jsonb)) h
    cross join q
    where na.highlights_tsv @@ q.tsq
      and h ->> 'description' is not null
      and to_tsvector('english', coalesce(h ->> 'description', '')) @@ q.tsq
  )
  select * from transcript_hits
  union all
  select * from description_hits
  union all
  select * from highlight_hits
  order by score desc
  limit p_limit;
$$;
