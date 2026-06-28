-- PERCEPTION-01 (roadmap §1.3): interactive perception tools cache + audit.
--
-- One row per perception call (frames / describe / contact_sheet) keyed by its
-- params, so repeated identical inspections are a cache hit instead of another
-- ffmpeg/Gemini round-trip. Outputs are immutable per params (frames and
-- contact sheets are deterministic; a describe answer is pinned to its range +
-- question), which is exactly what makes them cacheable.
--
--   kind     — 'frames' | 'describe' | 'contact_sheet'
--   params   — the normalized request params that define the cache key, e.g.
--              {t, n, interval} | {start, end, question} | {start, end, cols, rows}
--   result   — { frames:[{t,key}] } | { answer, model } | { key }
--              (R2 object keys — the web route presigns them on read)
--   cache_key — a stable string built from (clip_id, kind, params) for fast
--              upsert/lookup; unique so concurrent identical calls coalesce.

create table if not exists clip_inspections (
  id          uuid primary key default gen_random_uuid(),
  clip_id     uuid not null references clips(id) on delete cascade,
  kind        text not null,         -- frames | describe | contact_sheet
  cache_key   text not null,
  params      jsonb not null default '{}'::jsonb,
  result      jsonb,
  created_at  timestamptz not null default now()
);

create unique index if not exists clip_inspections_cache_key_idx
  on clip_inspections(cache_key);
create index if not exists clip_inspections_clip_id_idx
  on clip_inspections(clip_id);

-- RLS: scope each inspection to the owner of the clip's project (mirrors
-- visual_analyses).
alter table clip_inspections enable row level security;

create policy clip_inspections_owner_all on clip_inspections
  for all to authenticated
  using (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = clip_inspections.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = clip_inspections.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ));
