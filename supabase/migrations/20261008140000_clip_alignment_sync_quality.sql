-- Sync quality on clip_alignments, for automatic song ↔ clip sync (issue #160).
--
-- method     : 'xcorr' (same-take fast path, modal/song_sync.py) or 'dtw'
--              (chroma-DTW fallback, modal/music_align.py). Null on rows
--              computed before this column existed (those were all DTW).
-- confidence : the fast path's onset-envelope peak ratio (best / best rival).
--              >= 1.5 is trusted; recorded on DTW rows too, as the reason the
--              fast path was not used.
-- drift_ms   : spread of the offset re-measured in windows across the footage
--              (fast path only). > ~40 ms means a different take than the
--              master, so lip-sync will wander.
alter table clip_alignments add column if not exists method text;
alter table clip_alignments add column if not exists confidence double precision;
alter table clip_alignments add column if not exists drift_ms double precision;

create index if not exists clip_alignments_song_id_idx on clip_alignments(song_id);
