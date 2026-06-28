#!/bin/bash
# SessionStart hook for Claude Code on the web.
#
# Bootstraps a working dev environment so tests, linters, the CLI, and — most
# importantly — the live LyricSync dev API are usable out of the box:
#   1. ffmpeg            (media extraction / CLI / agent frame grabs)
#   2. web/ npm deps     (Next.js app: lint, build, `next dev`)
#   3. Python venv       (src/lyricsync editable install + test deps, no torch)
#   4. dev API smoke     (verify $LYRICSYNC_BASE_URL is reachable with the
#                         session's $LYRICSYNC_API_KEY so agents can hit it)
#
# Idempotent and non-interactive. Safe to re-run.
set -euo pipefail

ROOT="${CLAUDE_PROJECT_DIR:-$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)}"
cd "$ROOT"

log() { echo "[session-start] $*"; }

# ---------------------------------------------------------------------------
# 1. ffmpeg — needed by the CLI (extract/preview) and by agents extracting
#    frames from rendered output. Skip if already present.
# ---------------------------------------------------------------------------
if command -v ffmpeg >/dev/null 2>&1; then
  log "ffmpeg already present ($(ffmpeg -version | head -1 | awk '{print $3}'))"
elif command -v apt-get >/dev/null 2>&1; then
  log "installing ffmpeg via apt-get…"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq ffmpeg && log "ffmpeg installed" \
    || log "WARN: ffmpeg install failed (media/CLI ops will be unavailable)"
else
  log "WARN: no apt-get; ffmpeg not installed"
fi

# ---------------------------------------------------------------------------
# 2. web/ dependencies (Next.js). `npm install` (not `ci`) so the cached
#    container state is reused on the next session.
# ---------------------------------------------------------------------------
if [ -f web/package.json ]; then
  log "installing web/ npm deps…"
  npm install --prefix web --no-audit --no-fund --silent && log "web deps ready" \
    || log "WARN: npm install failed"
fi

# ---------------------------------------------------------------------------
# 3. Python: editable install of src/lyricsync + test deps, WITHOUT the heavy
#    whisperx/torch stack (only align.py needs it; nothing else does). This
#    makes the full tests/ suite importable and green in <1s.
# ---------------------------------------------------------------------------
if command -v uv >/dev/null 2>&1; then
  if [ ! -d .venv ]; then
    log "creating Python venv…"
    uv venv --python 3.11 .venv >/dev/null
  fi
  log "installing lyricsync (editable, no-deps) + test deps…"
  uv pip install --python .venv -q -e . --no-deps >/dev/null 2>&1 || true
  uv pip install --python .venv -q pytest httpx starlette "uvicorn[standard]" imageio-ffmpeg >/dev/null 2>&1 \
    && log "Python env ready (.venv)" \
    || log "WARN: Python dep install failed"
  # Put the venv on PATH for this session so `python`/`pytest` resolve to it.
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    {
      echo "export VIRTUAL_ENV=\"$ROOT/.venv\""
      echo "export PATH=\"$ROOT/.venv/bin:\$PATH\""
    } >> "$CLAUDE_ENV_FILE"
  fi
else
  log "WARN: uv not found; skipping Python setup"
fi

# ---------------------------------------------------------------------------
# 4. Dev API reachability — the thing agents actually talk to. Non-fatal: a
#    failure here just means the network policy or creds aren't wired, not that
#    the session should die.
# ---------------------------------------------------------------------------
if [ -n "${LYRICSYNC_BASE_URL:-}" ] && [ -n "${LYRICSYNC_API_KEY:-}" ]; then
  BASE="${LYRICSYNC_BASE_URL%/}"
  if [ -n "${CLAUDE_ENV_FILE:-}" ]; then
    echo "export LYRICSYNC_BASE=\"$BASE\"" >> "$CLAUDE_ENV_FILE"
  fi
  code="$(curl -sS -m 20 -o /dev/null -w '%{http_code}' \
    -H "Authorization: Bearer $LYRICSYNC_API_KEY" "$BASE/api/projects" 2>/dev/null || echo 000)"
  if [ "$code" = "200" ]; then
    log "dev API OK: $BASE (GET /api/projects → 200). Agents: see scripts/dev-api-smoke.sh"
  else
    log "WARN: dev API at $BASE returned HTTP $code (expected 200)"
  fi
else
  log "WARN: LYRICSYNC_BASE_URL / LYRICSYNC_API_KEY not set — dev API unavailable this session"
fi

log "done."
