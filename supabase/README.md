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

## One-time baseline (before the first CI run)

The production database **already has every historical migration applied**, so
the migration-history table must be told they're applied — otherwise the first
`db push` would try to replay them (and the non-idempotent `create table`s would
fail). Run once, locally, after installing the CLI and adding the secrets:

```bash
export SUPABASE_ACCESS_TOKEN=...        # same token as the secret
supabase link --project-ref ywfdqggvqrapwvxdzrfi   # prompts for DB password

# Mark every migration that is ALREADY in prod as applied, without running it.
# (Everything EXCEPT 20260620_add_stories_updated_at, which is not yet applied.)
supabase migration repair --status applied \
  20260101000000 20260102000000 20260103000000 \
  20260524000000 20260526000000 20260604000000 \
  20260607000000 20260610000000 20260610010000

supabase migration list   # confirm: all the above show as applied (remote),
                          # 20260620000000 shows as pending (local only)
```

After that, the next push to `main` (or a manual `supabase db push`) applies the
pending `20260620000000_add_stories_updated_at` migration — and every future
migration flows through CI automatically.
