-- Explicit Data API grants for every table in the public schema.
--
-- From 2026-10-30 Supabase stops granting anon/authenticated/service_role
-- access to new tables in `public` automatically. Production keeps the grants
-- its tables already have, but any database rebuilt from these migrations
-- (local `supabase db reset`, a preview branch, a new project) would end up
-- with tables that supabase-js / PostgREST can't reach ("permission denied").
--
-- This backfills the grants for every table the earlier migrations created,
-- so a fresh replay matches production. On production it's a no-op.
--
-- Roles:
--   authenticated — the web app's user sessions and API-key tokens
--                   (web/src/lib/supabase/server.ts, token.ts). RLS policies,
--                   all `to authenticated`, still scope rows to the owner.
--   service_role  — server-side code and Modal workers
--                   (web/src/lib/supabase/service.ts, modal/app.py).
--   anon          — deliberately not granted: no policy targets anon, so it
--                   could never see a row anyway.
--
-- New tables: grant in the same migration that creates them (see
-- supabase/README.md, "Data API grants"). scripts/check_migration_grants.py
-- enforces that in the DB migrate workflow.

grant select, insert, update, delete
  on all tables in schema public
  to authenticated, service_role;
