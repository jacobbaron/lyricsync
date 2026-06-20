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

Once the secrets are set, **no manual steps are needed** — see baselining below.

## Baselining (automatic, in CI)

The production database **already has every historical migration applied**, but
the migration-history table doesn't know it. If CI just ran `db push`, it would
try to replay all of history and the non-idempotent `create table`s would fail.

The workflow handles this itself: a **Baseline** step runs
`supabase migration repair --status applied …` for the pre-existing migrations
before `db push`, marking them applied without re-running them. It's idempotent,
so it's safe on every run. The one migration deliberately left off that list —
`20260620000000_add_stories_updated_at` — is therefore the only pending one, and
the apply step applies it on the first run.

Net effect: add the two secrets, merge to `main`, and the pipeline baselines
prod and applies `updated_at` automatically. Every future migration then flows
through CI with no manual action.

If you ever need to baseline by hand instead (e.g. running the CLI locally):

```bash
export SUPABASE_ACCESS_TOKEN=...        # same token as the secret
supabase link --project-ref ywfdqggvqrapwvxdzrfi   # prompts for DB password
supabase migration repair --status applied \
  20260101000000 20260102000000 20260103000000 \
  20260524000000 20260526000000 20260604000000 \
  20260607000000 20260610000000 20260610010000
supabase migration list   # historical = applied; 20260620000000 = pending
```
