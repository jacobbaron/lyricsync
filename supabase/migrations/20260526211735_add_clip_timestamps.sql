-- Applied 2026-05-26 via Supabase MCP
-- recorded_at: real recording wall-clock instant from video metadata
--   (ffprobe creation_time / Apple QuickTime creationdate), set by the
--   transcribe worker. Null when the source has no usable timestamp.
-- created_at:  upload time, used as the timestamp fallback when recorded_at
--   is null. Backfilled to now() for pre-existing rows.
ALTER TABLE clips ADD COLUMN IF NOT EXISTS recorded_at timestamptz;
ALTER TABLE clips ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();
