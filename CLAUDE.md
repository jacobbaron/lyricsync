# lyricsync — Claude Code context

## Architecture

- **Frontend/API**: Next.js 15 (App Router) on Vercel — `web/`
- **Background compute**: Modal — `modal/app.py`
- **Database**: Supabase Postgres — migrations in `db/migrations/`
- **Storage**: Cloudflare R2

## Available tools in Claude Code on the web

The following MCP servers are configured and available. Use ToolSearch to load schemas before calling.

### GitHub (`mcp__github__*`)
Full GitHub API: PRs, issues, branches, Actions runs/logs, file contents.
- Create PRs: `mcp__github__create_pull_request`
- Merge PRs: `mcp__github__merge_pull_request`
- List/check Actions runs: `mcp__github__actions_list`
- Get job logs: `mcp__github__get_job_logs`
- Trigger workflows: `mcp__github__actions_run_trigger` (note: dispatch may be 403 — push to main instead)

### Supabase (`mcp__Supabase__*`)
Direct DB access including raw SQL.
- Run queries: `mcp__Supabase__execute_sql` (project_id: `ywfdqggvqrapwvxdzrfi`)
- Apply migrations: `mcp__Supabase__apply_migration`

### Vercel (`mcp__Vercel__*`)
Deployment status and runtime logs.
- List deployments: `mcp__Vercel__list_deployments` (teamId: `team_So8JEPVFyvQLqx3mubnoLLon`, projectId: `prj_K5m5YMl7JOi22rtr8ehWU4EtOagM`)
- Runtime logs: `mcp__Vercel__get_runtime_logs`
- List teams: `mcp__Vercel__list_teams`

### Cloudflare (`mcp__Cloudflare_Developer_Platform__*` and `mcp__cloudflare__*`)
R2, KV, Workers, D1.

## Deployment

### Vercel (frontend + API routes)
Auto-deploys from `main`. No manual step needed.

### Modal (background workers)
**Cannot be deployed directly from this environment — no Modal token stored here.**

Use the GitHub Action instead:

```bash
# Trigger by pushing any change to modal/ on main
git add modal/app.py && git commit -m "..." && git push origin main
```

The `deploy-modal.yml` workflow fires on any push to `main` that touches `modal/**`.
It uses `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` stored as GitHub secrets.

To manually trigger: push a no-op commit to `modal/app.py` (e.g., append a blank line).

The `workflow_dispatch` trigger exists but the GitHub MCP integration currently returns 403 for dispatch — use the push approach instead.

## Key secrets

| Secret | Where | Purpose |
|---|---|---|
| `MODAL_WEBHOOK_SECRET` | Vercel env | Authenticates Vercel → Modal calls |
| `MODAL_EDIT_URL` | Vercel env | Modal `edit_timeline` endpoint (timeline edit ops, see docs/timeline_editing.md) |
| `MODAL_TOKEN_ID` / `MODAL_TOKEN_SECRET` | GitHub secrets | Modal deploy in GHA |
| `GEMINI_API_KEY` | Modal secret `lyricsync-secrets` | Gemini visual analysis |
| `SUPABASE_URL` / `SUPABASE_SERVICE_ROLE_KEY` | Modal secret `lyricsync-secrets` | DB access from Modal workers |
| `CLOUDFLARE_R2_*` / `R2_BUCKET_NAME` / `R2_ENDPOINT` | Modal secret `lyricsync-secrets` | R2 storage from Modal |

To update Modal secrets:
```bash
modal secret create lyricsync-secrets KEY=value --force
```

## Modal image notes

Every Modal image that runs functions from `app.py` must mount **all** local helper modules it imports:

```python
.add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
.add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
.add_local_file(Path(__file__).parent / "visual.py", "/root/visual.py")
```

`app.py` imports `transcript` and `timeline` at module level; Modal's cloudpickle captures these globals and the container crash-loops on startup if a file is missing.

## Supabase project

- Project ID: `ywfdqggvqrapwvxdzrfi`
- Region: `us-east-1`
- DB host: `db.ywfdqggvqrapwvxdzrfi.supabase.co`

## Common patterns

### Check if a Modal deploy succeeded
```bash
# Via GHA job list — look for deploy-modal.yml run on the commit
mcp__github__actions_list method=list_workflow_runs resource_id=deploy-modal.yml
```

### Debug a stuck visual analysis
```sql
SELECT id, variant, status, error, debug->>'steps', debug->>'traceback'
FROM visual_analyses
WHERE clip_id = '<clip-uuid>'
ORDER BY created_at DESC
LIMIT 10;
```

### Fire the four VIS-01 variants
```bash
CLIP=<clip-uuid>
for v in flash flash_lowres pro editorial; do
  curl -sL -XPOST -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
    -H 'Content-Type: application/json' \
    "$LYRICSYNC_BASE_URL/api/clips/$CLIP/analyze" \
    -d "{\"variant\":\"$v\"}"
done
```

---

# Agent operational playbook (operating the app, not deploying it)

The notes above are for *deploying*; this is for *inspecting and editing*
LyricSync content via the live app.

## App API key + base URL are already in the web-session env

```bash
BASE="${LYRICSYNC_BASE_URL%/}"        # may have a trailing slash — strip it
curl -sS -H "Authorization: Bearer $LYRICSYNC_API_KEY" "$BASE/api/clips/$CLIP/visual"
```
`LYRICSYNC_API_KEY` (an `lsk_…` bearer token) and `LYRICSYNC_BASE_URL` are
exported in web sessions — no setup. Two ways in: the **REST API** (real code
paths — prefer for writes/renders) or **Supabase MCP** `execute_sql`
(project_id `ywfdqggvqrapwvxdzrfi`) for direct reads/bulk queries.

## Data model

`projects → clips → stories`, plus `visual_analyses`, `generation_rounds`.
- **clips**: `filename` (e.g. `IMG_2427.mov`) is what edits reference as
  `source`; `visual_description` holds the `context` visual summary.
- **stories**: the edit lives in **`ranges_json`** (legacy) and/or
  **`timeline_json`** (the EDL the render worker prefers; see
  `modal/timeline.py` + `docs/timeline_editing.md`).

## The edit formats

`ranges_json` — ordered array of `{source, start, end, overlay?}`. `source` is
a clip `filename` or `"blank"` (black card); `start`/`end` are seconds within
the source. `overlay` → ffmpeg drawtext title card:
`{text, in, out, size, position(center|upper|lower), wrap}`. Extra keys pass
through untouched.

`timeline_json` (EDL v1) — `{version, width, height, fps, tracks:[video, text]}`.
Setting `timeline_json` on a story overrides `ranges_json` at render time. This
is how you control the **output frame**: e.g. set `width/height` to `1080x1440`
for 3:4 footage. Per-clip video-item knobs: `speed` (0.25–20×, slow-mo / fast / time-lapse)
and `audio_fx` (`echo` | `reverb` | `cavern` — `aecho`-based echo/reverb wash,
e.g. for exaggerated "bad room" gags).

⚠️ **Vocabulary:** "crop" here means **trimming the time range** (`start`/`end`),
**not** reframing the picture.

## Output canvas / letterboxing

The render frame defaults to portrait **1080×1920**. When a story uses that
default frame, the render worker now **auto-fits the canvas to the source
clips' aspect** (`choose_canvas` in `timeline.py`, fed by `ffprobe` display
dims) — uniform 3:4 / 4:3 / landscape footage renders bar-free; mixed-aspect
projects keep the 1080×1920 default. A non-default `timeline_json` canvas is
always respected, so you can pin a frame explicitly.

## Rendering — every edit is persisted

No transient renders. `POST /api/projects/[id]/stories {ranges}` **creates** a
story then renders; `POST /api/stories/[id]/render {ranges?}` **updates** ranges
(or, with an empty body, re-renders the current `timeline_json` in place) — so
any cut is recoverable from the DB. Modal renders by reading the row by
`story_id`.

## Watching / fetching outputs + sending a link to the user

```bash
curl -sS -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
  "$BASE/api/stories/$STORY/signed-url"   # → {playback_url, download_url}, ~1h
```
Returns `409` until `status=done`. **To give the user a video:** hand them the
**`download_url`** as a **clickable Markdown link** —
`[⬇ Download (output.mp4)](<download_url>)` — one-click, no sign-in. Do *not*
use `SendUserFile`, the login-gated `/stories/[id]` page, or a raw pasted URL;
all have failed for the user. The `/api/stories/[id]/video` route needs a
browser session (401 with an API key).

## Transcripts (match overlay copy to what's said)

`GET /api/projects/[id]/transcript` → `{words:[{text, global_start/end,
local_start/end, source, …}]}`. Filter by `source` (filename); `local_*` are
times within that clip — use them directly for range `start`/`end` and overlay
`in`/`out`.

## Gotchas

- **Cloudflare R2 MCP** `r2_buckets_list` → 403 (token lacks R2 perms); read
  objects via the app's signed URLs instead.
- **Ephemeral container:** commit + push or lose local edits (DB/R2 persist).
- **Deploy reality:** Modal renders from whatever's on `main` (deploy-modal.yml).
  Check `mcp__github__actions_list resource_id=deploy-modal.yml` for the
  deployed commit before assuming a branch's `modal/` is what's live.
