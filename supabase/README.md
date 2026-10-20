# Database migrations

SQL migrations live in `supabase/migrations/` and are applied with the
[Supabase CLI](https://supabase.com/docs/guides/local-development) through the
**DB migrate** GitHub Action (`.github/workflows/db-migrate.yml`):

- **PR** → `supabase db push --dry-run` prints the migrations that *would* run
  (review gate; no DB writes).
- **Merge to `main`** → `supabase db push` applies any pending migrations.

This replaces applying schema changes by hand through the Supabase MCP / SQL
editor. The CLI tracks applied migrations in the `supabase_migrations.schema_migrations`
table, so each file runs exactly once.

## Adding a migration

1. Create a file named `supabase/migrations/<YYYYMMDDHHMMSS>_short_name.sql`
   (14-digit UTC timestamp prefix — this is the apply order). Locally you can
   run `supabase migration new short_name` to get the name.
2. Write idempotent SQL where practical (`add column if not exists`,
   `create ... if not exists`, `create or replace`). Destructive/locking
   changes deserve extra care on large tables.
3. **New table in `public`? Grant Data API access in the same migration**
   (see below). The workflow fails the PR if you don't.
4. Open a PR. The dry-run shows what will apply. Merge → it ships.

## Data API grants (required for every new table)

Since 2026-10-30 Supabase no longer grants API roles access to new tables in
`public`. Without a grant, supabase-js / PostgREST returns "permission denied"
at runtime. So every `create table` gets, in the same file:

```sql
alter table public.my_table enable row level security;
create policy my_table_owner_all on public.my_table for all to authenticated using (...);

grant select, insert, update, delete on public.my_table to authenticated, service_role;
```

- `authenticated`: the web app's user sessions and API-key tokens (RLS scopes rows).
- `service_role`: server routes and Modal workers (bypasses RLS).
- `anon`: don't grant unless a policy actually targets `anon`; none do today.

`scripts/check_migration_grants.py` enforces this in the DB migrate workflow
(run it locally the same way). Tables from migrations up to
`20261020130000_data_api_grants` are covered by that backfill migration, which
also makes local `supabase db reset` and preview branches match production.

## Required GitHub secrets

| Secret | What |
|---|---|
| `SUPABASE_ACCESS_TOKEN` | Personal access token — https://supabase.com/dashboard/account/tokens |
| `SUPABASE_DB_PASSWORD`  | The project's database password |

Project ref `ywfdqggvqrapwvxdzrfi` is hard-coded in the workflow.

Once the secrets are set, **no manual steps are needed**.

## History alignment (already done — context)

The historical migrations were applied over time via the Supabase MCP, which
recorded each one in `supabase_migrations.schema_migrations` with an
auto-generated timestamp version. The repo's migration **filenames carry those
exact recorded version numbers**, so the CLI already sees them as applied — no
baseline/repair step is needed. `db push` therefore pushes only genuinely-new
migrations (the first such being `20260620000000_add_stories_updated_at`).

The version ↔ migration mapping (for reference):

| Version | Migration |
|---|---|
| `20260524023159` | initial_schema |
| `20260524201440` | add_error_message |
| `20260525022013` | generation_rounds |
| `20260526205334` | api_keys |
| `20260526211735` | add_clip_timestamps |
| `20260606122313` | add_visual_analyses |
| `20260607165445` | add_clip_visual_description |
| `20260610131740` | add_story_timeline |
| `20260610132954` | add_round_debug |

When adding a **new** migration, just use a current UTC timestamp
(`supabase migration new <name>`); it will sort after these and push cleanly.
