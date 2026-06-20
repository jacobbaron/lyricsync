-- Roadmap 1.1: record the exact perception document (transcript + interleaved
-- visual annotations) each generation round saw, for later eval/regression
-- comparison. Written best-effort by the generate worker after a successful
-- round.

alter table generation_rounds
  add column if not exists debug jsonb;
