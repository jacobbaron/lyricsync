-- P2-01: Generation rounds + story Phase 2 columns
--
-- Layers the Phase 2 tables/columns on top of the baseline (initial_schema)
-- migration.

-- 1. Add story_prompt to projects
alter table projects
  add column if not exists story_prompt text;

-- 2. Create generation_rounds table
create table if not exists generation_rounds (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  round       int  not null,
  prompt      text,
  created_at  timestamptz not null default now()
);

create index if not exists generation_rounds_project_id_idx
  on generation_rounds(project_id);

-- 3. Add Phase 2 columns to stories
alter table stories
  add column if not exists generation_round_id     uuid references generation_rounds(id) on delete cascade,
  add column if not exists estimated_duration_secs double precision;

create index if not exists stories_generation_round_id_idx
  on stories(generation_round_id);

-- 4. RLS for generation_rounds
alter table generation_rounds enable row level security;

create policy generation_rounds_owner_all on generation_rounds
  for all to authenticated
  using (exists (
    select 1 from projects p
    where p.id = generation_rounds.project_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from projects p
    where p.id = generation_rounds.project_id
      and p.owner = (auth.jwt() ->> 'email')
  ));
