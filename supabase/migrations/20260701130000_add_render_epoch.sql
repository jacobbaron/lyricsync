-- History backfill: this migration was applied to the remote DB directly
-- (via the Supabase MCP, version 20260701130000 "add_render_epoch") during the
-- music-sync work but its file was never committed, so `supabase db push`
-- reported "remote migration versions not found in local migrations directory"
-- and refused to push anything (blocking all later migrations).
--
-- Re-committing the exact recorded statement realigns the repo with the
-- schema_migrations history: the CLI already has this version marked applied,
-- so it is skipped on push (no-op against prod) and pending migrations flow
-- again. The statement is idempotent, so it is also correct on a fresh DB.
--
-- See supabase/README.md "History alignment".

alter table stories
  add column if not exists render_epoch bigint not null default 0;
