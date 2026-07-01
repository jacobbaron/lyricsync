-- Songs: a finished full-band mix uploaded for a project, laid under footage by
-- the music-sync feature (see modal/music_align.py + timeline.music). A song is
-- a project-level asset — many stories in a project reference the same song by
-- id from their timeline_json.music. Mirrors clips for storage/ownership.
--
-- status values:
--   'uploading'  — presigned PUT in flight (r2_key set once known, at complete)
--   'ready'      — object is in R2, safe to align/render against
--   'error'      — terminal failure
create table if not exists songs (
  id            uuid primary key default gen_random_uuid(),
  project_id    uuid not null references projects(id) on delete cascade,
  r2_key        text,
  filename      text,
  duration_secs double precision,
  status        text not null default 'uploading',
  created_at    timestamptz not null default now()
);

create index if not exists songs_project_id_idx on songs(project_id);

alter table songs enable row level security;

-- Owner (via the parent project) may do anything; mirrors clips_owner_all.
create policy songs_owner_all on songs
  for all to authenticated
  using (exists (select 1 from projects p where p.id = songs.project_id and p.owner = (auth.jwt() ->> 'email')))
  with check (exists (select 1 from projects p where p.id = songs.project_id and p.owner = (auth.jwt() ->> 'email')));

-- Per-story music config, kept out of timeline_json so timeline edit ops can't
-- clobber it. Shape mirrors timeline.music (see modal/timeline.py _validate_music):
--   {song_id, song_start, gain_db?, scratch_gain_db?, cost?}
-- The render worker injects this as timeline["music"] before compiling.
alter table stories
  add column if not exists music_json jsonb;
