-- clip_alignments: durable, reusable metadata mapping a clip's footage window to
-- a song's timeline (music sync). song_start is the song time that lines up with
-- footage_start (so song_time = footage_time - footage_start + song_start). A cut
-- reuses this to lip-sync that footage to a music bed of the same song, and the
-- offset can be nudged independently of any render. Computed by the align worker
-- (chroma-DTW); see modal/app.py + modal/music_align.py.
--
-- status: 'aligning' (worker running) | 'ready' (song_start set) | 'error'.
create table if not exists clip_alignments (
  id            uuid primary key default gen_random_uuid(),
  clip_id       uuid not null references clips(id) on delete cascade,
  song_id       uuid not null references songs(id) on delete cascade,
  footage_start double precision not null,
  footage_end   double precision not null,
  song_start    double precision,
  cost          double precision,
  status        text not null default 'aligning',
  error         text,
  created_at    timestamptz not null default now()
);

create index if not exists clip_alignments_clip_id_idx on clip_alignments(clip_id);
create index if not exists clip_alignments_clip_song_idx
  on clip_alignments(clip_id, song_id);

alter table clip_alignments enable row level security;

-- Owner (via the clip's parent project) may do anything; mirrors clips_owner_all.
create policy clip_alignments_owner_all on clip_alignments
  for all to authenticated
  using (exists (
    select 1 from clips c join projects p on p.id = c.project_id
    where c.id = clip_alignments.clip_id and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from clips c join projects p on p.id = c.project_id
    where c.id = clip_alignments.clip_id and p.owner = (auth.jwt() ->> 'email')
  ));
