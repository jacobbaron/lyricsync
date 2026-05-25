-- Core schema for lyricsync web app.
--
-- This file is the source of truth for the database structure.
-- To apply to a fresh Supabase project, run this file in the SQL editor,
-- then run db/migrations/p2-01-generation-rounds.sql for Phase 2 additions.
--
-- All tables enable Row Level Security. Policies scope every row to the
-- authenticated user's email via the JWT, so the anon/browser session can
-- only read/write rows owned by the logged-in user. Server-side code that
-- uses the service-role key bypasses RLS by design.

create extension if not exists "pgcrypto";

-- ---------------------------------------------------------------------------
-- Tables
-- ---------------------------------------------------------------------------

-- Project: a collection of source clips, transcripts, and stories.
--
-- status values:
--   'uploading'           — at least one clip is still uploading to R2
--   'transcribing'        — all clips uploaded; per-clip Whisper jobs running
--   'transcribed'         — alignment/merge done; transcript is readable
--   'generating_stories'  — Claude is generating story options (Phase 2)
--   'stories_ready'       — story options are ready for user selection (Phase 2)
--   'rendering'           — a cut is being rendered
--   'done'                — a cut has finished rendering
--   'error'               — terminal failure; see app logs
create table projects (
  id            uuid primary key default gen_random_uuid(),
  owner         text not null,
  name          text not null,
  status        text not null default 'uploading',
  story_prompt  text,          -- optional user prompt for LLM story generation
  error_message text,
  created_at    timestamptz not null default now()
);

-- Clip: a single source video uploaded by the user.
--
-- status values:
--   'uploading'           — presigned PUT in flight or not yet started
--   'uploading_complete'  — client confirmed PUT succeeded; awaiting transcribe kickoff
--   'transcribing'        — Whisper job running
--   'aligned'             — transcript merged into the project timeline
--   'error'               — terminal failure for this clip
create table clips (
  id                uuid primary key default gen_random_uuid(),
  project_id        uuid not null references projects(id) on delete cascade,
  r2_key            text,
  filename          text,
  duration_secs     double precision,
  global_start      double precision,
  transcript_r2_key text,
  status            text not null default 'uploading',
  error_message     text
);

-- GenerationRound: one round of LLM story generation for a project.
-- Each round corresponds to one Claude API call. Rounds accumulate into a
-- conversation history that subsequent rounds receive as context.
--
-- round: 1-based sequence number within the project.
-- prompt: the user's natural-language request for this round (may be null
--         for round 1 if the user submitted without typing anything).
create table generation_rounds (
  id          uuid primary key default gen_random_uuid(),
  project_id  uuid not null references projects(id) on delete cascade,
  round       int  not null,
  prompt      text,
  created_at  timestamptz not null default now()
);

-- Story: a proposed or rendered cut (set of time ranges).
-- Stories created by the manual range picker (Phase 1) are inserted with
-- status='rendering' and no generation_round_id.
-- Stories created by the LLM (Phase 2) start at status='generating' while
-- Claude is working, then advance to 'ready' when ranges are available, and
-- finally 'rendering' → 'done' when the user triggers the render.
--
-- status values:
--   'generating'   — (Phase 2) LLM is producing ranges; title/ranges not yet set
--   'ready'        — (Phase 2) LLM has returned ranges; awaiting user render trigger
--   'rendering'    — render job running
--   'done'         — output MP4 is in R2 (render_r2_key set)
--   'error'        — terminal failure
create table stories (
  id                      uuid primary key default gen_random_uuid(),
  project_id              uuid not null references projects(id) on delete cascade,
  generation_round_id     uuid references generation_rounds(id) on delete cascade,
  title                   text,
  description             text,
  estimated_duration_secs double precision,
  ranges_json             jsonb,
  render_r2_key           text,
  status                  text not null default 'generating',
  error_message           text,
  created_at              timestamptz not null default now()
);

-- Feedback: free-text notes attached to a story, used as post-render context
-- when iterating in Phase 3. Not consumed in Phase 2.
create table feedback (
  id         uuid primary key default gen_random_uuid(),
  story_id   uuid not null references stories(id) on delete cascade,
  text       text not null,
  created_at timestamptz not null default now()
);

-- ---------------------------------------------------------------------------
-- Indexes
-- ---------------------------------------------------------------------------

create index clips_project_id_idx             on clips(project_id);
create index generation_rounds_project_id_idx on generation_rounds(project_id);
create index stories_project_id_idx           on stories(project_id);
create index stories_generation_round_id_idx  on stories(generation_round_id);
create index feedback_story_id_idx            on feedback(story_id);
create index projects_owner_idx               on projects(owner);

-- ---------------------------------------------------------------------------
-- Row Level Security
-- ---------------------------------------------------------------------------

alter table projects          enable row level security;
alter table clips             enable row level security;
alter table generation_rounds enable row level security;
alter table stories           enable row level security;
alter table feedback          enable row level security;

create policy projects_owner_all on projects
  for all to authenticated
  using (owner = (auth.jwt() ->> 'email'))
  with check (owner = (auth.jwt() ->> 'email'));

create policy clips_owner_all on clips
  for all to authenticated
  using (exists (select 1 from projects p where p.id = clips.project_id and p.owner = (auth.jwt() ->> 'email')))
  with check (exists (select 1 from projects p where p.id = clips.project_id and p.owner = (auth.jwt() ->> 'email')));

create policy generation_rounds_owner_all on generation_rounds
  for all to authenticated
  using (exists (select 1 from projects p where p.id = generation_rounds.project_id and p.owner = (auth.jwt() ->> 'email')))
  with check (exists (select 1 from projects p where p.id = generation_rounds.project_id and p.owner = (auth.jwt() ->> 'email')));

create policy stories_owner_all on stories
  for all to authenticated
  using (exists (select 1 from projects p where p.id = stories.project_id and p.owner = (auth.jwt() ->> 'email')))
  with check (exists (select 1 from projects p where p.id = stories.project_id and p.owner = (auth.jwt() ->> 'email')));

create policy feedback_owner_all on feedback
  for all to authenticated
  using (exists (select 1 from stories s join projects p on p.id = s.project_id where s.id = feedback.story_id and p.owner = (auth.jwt() ->> 'email')))
  with check (exists (select 1 from stories s join projects p on p.id = s.project_id where s.id = feedback.story_id and p.owner = (auth.jwt() ->> 'email')));
