-- PERCEPTION T1: shared technical-quality / camera-motion / detection signals
-- table. One row per (clip, kind) analysis run, mirroring visual_analyses, so
-- later perception tickets (camera motion, detection) reuse this table/RLS
-- instead of growing a new one per signal type.
--
--   kind   — 'quality' (this ticket) | 'camera_motion' | 'detection' | ...
--   result — compact parsed summary, shape depends on `kind`
--            (quality: {summary:{mean_usable, flagged_seconds, ...},
--                       flagged_spans:[{start,end,reasons}]})
--   result_r2_key — full per-second/per-frame sidecar JSON also written to R2
--   debug  — diagnostic blob (ffmpeg steps, timings, traceback on error)

create table if not exists clip_signals (
  id            uuid primary key default gen_random_uuid(),
  clip_id       uuid not null references clips(id) on delete cascade,
  kind          text not null,
  status        text not null default 'processing',  -- processing | done | error
  result        jsonb,
  result_r2_key text,
  debug         jsonb,
  error         text,
  created_at    timestamptz not null default now()
);

create index if not exists clip_signals_clip_id_kind_idx
  on clip_signals(clip_id, kind);

-- RLS: scope each signal to the owner of the clip's project (mirrors
-- visual_analyses).
alter table clip_signals enable row level security;

create policy clip_signals_owner_all on clip_signals
  for all to authenticated
  using (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = clip_signals.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = clip_signals.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ));
