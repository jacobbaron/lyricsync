-- lyric_alignments: force-aligned lyric timings for a clip.
--
-- The caller submits the lyrics they already have; the worker solves only for
-- timing (wav2vec2 forced alignment), so the text is never invented by ASR.
-- That is the only thing that works on sung audio, where transcription
-- mishears words, drops lines under loud instrumentation, and loops on
-- repeated refrains. See modal/lyric_align.py + docs/lyric_alignment.md.
--
-- result: {lines: [{text, start, end, score}], windows, anchored_windows,
--          coverage, mean_score} — start/end are clip-local seconds, ready to
-- drop onto a timeline text track.
--
-- status: 'aligning' (worker running) | 'ready' (result set) | 'error'.
create table if not exists lyric_alignments (
  id         uuid primary key default gen_random_uuid(),
  clip_id    uuid not null references clips(id) on delete cascade,
  lyrics     text not null,
  result     jsonb,
  status     text not null default 'aligning',
  error      text,
  created_at timestamptz not null default now()
);

create index if not exists lyric_alignments_clip_id_idx
  on lyric_alignments(clip_id, created_at desc);

alter table lyric_alignments enable row level security;

-- Owner (via the clip's parent project) may do anything; mirrors
-- clip_alignments_owner_all.
create policy lyric_alignments_owner_all on lyric_alignments
  for all to authenticated
  using (exists (
    select 1 from clips c join projects p on p.id = c.project_id
    where c.id = lyric_alignments.clip_id and p.owner = (auth.jwt() ->> 'email')
  ))
  with check (exists (
    select 1 from clips c join projects p on p.id = c.project_id
    where c.id = lyric_alignments.clip_id and p.owner = (auth.jwt() ->> 'email')
  ));
