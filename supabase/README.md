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
3. Open a PR. The dry-run shows what will apply. Merge → it ships.

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
