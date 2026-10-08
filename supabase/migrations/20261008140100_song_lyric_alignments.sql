-- Lyric alignment against a song master (issue #160).
--
-- A lyric_alignments row now targets either a clip (as before; result times are
-- clip-local) or a song (result times are song time). Exactly one is set.
-- songs.transcript_r2_key caches the song's ASR transcript, which the lyric
-- worker uses only as anchor evidence for windowing (never as text).
alter table lyric_alignments add column if not exists song_id uuid
  references songs(id) on delete cascade;
alter table lyric_alignments alter column clip_id drop not null;

do $$
begin
  if not exists (
    select 1 from pg_constraint where conname = 'lyric_alignments_one_target'
  ) then
    alter table lyric_alignments add constraint lyric_alignments_one_target
      check ((clip_id is null) <> (song_id is null));
  end if;
end $$;

create index if not exists lyric_alignments_song_id_idx
  on lyric_alignments(song_id, created_at desc);

alter table songs add column if not exists transcript_r2_key text;

-- Owner (via the clip's or the song's parent project) may do anything.
drop policy if exists lyric_alignments_owner_all on lyric_alignments;
create policy lyric_alignments_owner_all on lyric_alignments
  for all to authenticated
  using (
    exists (
      select 1 from clips c join projects p on p.id = c.project_id
      where c.id = lyric_alignments.clip_id and p.owner = (auth.jwt() ->> 'email')
    )
    or exists (
      select 1 from songs s join projects p on p.id = s.project_id
      where s.id = lyric_alignments.song_id and p.owner = (auth.jwt() ->> 'email')
    )
  )
  with check (
    exists (
      select 1 from clips c join projects p on p.id = c.project_id
      where c.id = lyric_alignments.clip_id and p.owner = (auth.jwt() ->> 'email')
    )
    or exists (
      select 1 from songs s join projects p on p.id = s.project_id
      where s.id = lyric_alignments.song_id and p.owner = (auth.jwt() ->> 'email')
    )
  );
