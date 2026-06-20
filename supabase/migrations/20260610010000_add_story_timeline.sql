-- EDL-01: editable timeline (EDL) model for stories.
--
-- stories.timeline_json holds the versioned timeline (see modal/timeline.py
-- for the schema). ranges_json remains as the legacy seed: the first edit (or
-- a render with no timeline) materializes the timeline from it, after which
-- the timeline is the single source of truth for rendering.
--
-- story_revisions keeps a full snapshot per edit, giving undo/redo and an
-- audit trail of how the LLM evolved a cut.

alter table stories
  add column if not exists timeline_json     jsonb,
  add column if not exists timeline_revision int not null default 0;

create table if not exists story_revisions (
  id         uuid primary key default gen_random_uuid(),
  story_id   uuid not null references stories(id) on delete cascade,
  revision   int  not null,
  ops        jsonb,           -- the edit ops that produced this revision
  timeline   jsonb not null,  -- full timeline snapshot after applying ops
  created_at timestamptz not null default now()
);

create unique index if not exists story_revisions_story_rev_uq
  on story_revisions(story_id, revision);

-- RLS: scope each revision to the owner of the story's project (mirrors stories).
alter table story_revisions enable row level security;

create policy story_revisions_owner_all on story_revisions
  for all to authenticated
  using (exists (
    select 1 from stories s
    join projects p on p.id = s.project_id
    where s.id = story_revisions.story_id
      and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from stories s
    join projects p on p.id = s.project_id
    where s.id = story_revisions.story_id
      and p.owner = (auth.jwt() ->> 'email')
  ));
