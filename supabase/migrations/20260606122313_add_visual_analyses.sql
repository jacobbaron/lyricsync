-- VIS-01: visual analysis experimentation harness.
--
-- One row per (clip, variant) analysis run. Storing each run separately — rather
-- than a single column on clips — lets us A/B different Gemini approaches
-- (model / prompt strategy / media resolution) on the same clip and compare
-- them side by side. `debug` holds the full Gemini round-trip for diagnosis
-- while this is still in development.

create table if not exists visual_analyses (
  id            uuid primary key default gen_random_uuid(),
  clip_id       uuid not null references clips(id) on delete cascade,
  variant       text not null default 'flash',
  status        text not null default 'analyzing',  -- analyzing | done | error
  result        jsonb,         -- parsed { summary, segments, highlights, suggested_clips }
  result_r2_key text,          -- parsed track also written to R2 for parity
  debug         jsonb,         -- prompt, raw_response, usage, timings, file state, traceback
  error         text,
  created_at    timestamptz not null default now()
);

create index if not exists visual_analyses_clip_id_idx on visual_analyses(clip_id);

-- RLS: scope each analysis to the owner of the clip's project (mirrors clips).
alter table visual_analyses enable row level security;

create policy visual_analyses_owner_all on visual_analyses
  for all to authenticated
  using (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = visual_analyses.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from clips c
    join projects p on p.id = c.project_id
    where c.id = visual_analyses.clip_id
      and p.owner = (auth.jwt() ->> 'email')
  ));
