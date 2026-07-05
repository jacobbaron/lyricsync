---
name: testing-lyricsync-api
description: Test lyricsync Next.js API routes (e.g. /api/clips/[id]/analyze) end-to-end against the PR's Vercel preview using an API key. Use when verifying API/behavioral changes to web/src/app/api routes.
---

# Testing lyricsync API changes end-to-end

The API is API-key callable (`Authorization: Bearer lsk_…`). This is the intended
path for scripted testing — prefer curl over the browser for API-route changes.

## Environments
- **PROD** = `main`: `https://lyricsync-ten.vercel.app` (value in `LYRICSYNC_BASE_URL`,
  which may lack the scheme — prepend `https://`). Runs the *old* code → use as the
  "before" baseline.
- **PR preview** (new code): grab the `*.vercel.app` preview URL from the Vercel bot
  comment on the PR (`git_view_pr`). It runs the branch's `web/` code.
- Backend (Supabase/R2/Modal) is shared, so real clips resolve on both, and writes
  hit the real DB. Modal workers deploy from `main` only (deploy-modal.yml), so
  `modal/**` changes are NOT exercised by a preview — validate those with
  `python -m py_compile modal/app.py` + `pytest tests/ -q`.

## Vercel Deployment Protection (the main gotcha)
Preview deployments are SSO-protected: an API-key request gets a `302` to
`vercel.com/sso-api`. Bypass with the "Protection Bypass for Automation" secret:
```
curl -H "Authorization: Bearer $LYRICSYNC_API_KEY" \
     -H "x-vercel-protection-bypass: $VERCEL_AUTOMATION_BYPASS_SECRET" \
     -H 'Content-Type: application/json' \
     -XPOST "$PREVIEW/api/clips/$CLIP/analyze" -d '{"variant":"flash"}'
```
If protection is off, the header is simply ignored.

## Finding test data
- List projects: `GET /api/projects`. Project detail `GET /api/projects/{id}` lists
  clips but **omits `r2_key`** — a clip with `status:"aligned"` (or later) has an
  uploaded video, so the analyze 202 path works; a clip that 409s ("no uploaded
  video yet") lacks `r2_key` — pick another.
- Request-validation errors (e.g. 400 on a bad body field) fire *before* the clip
  lookup, so they need no valid clip (use a dummy UUID) and write nothing.

## Evidence
Capture status + body + headers: `curl -sS -D /tmp/h.txt -o /tmp/b.txt -w "HTTP %{http_code}\n" ...`
then `grep -i '^warning:' /tmp/h.txt`. Response headers matter for this API (e.g.
deprecation `Warning: 299 - "..."`). Present PROD vs PREVIEW side-by-side.

## Cost awareness
A 202 from `/analyze` creates a real `visual_analyses` row and fires a Gemini flash
call. Keep 202 tests minimal and get user approval before firing several.

## Devin Secrets Needed
- `LYRICSYNC_API_KEY` — Bearer token (`lsk_…`).
- `LYRICSYNC_BASE_URL` — prod base (may lack scheme).
- `VERCEL_AUTOMATION_BYPASS_SECRET` — Vercel Protection Bypass for Automation
  (repo-scoped) to reach protected preview deployments.
