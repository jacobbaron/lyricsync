#!/usr/bin/env bash
# End-to-end smoke test for the LyricSync dev API.
#
# Proves a Claude agent (or a human) can talk to the live dev API using the
# session-provided creds and walk the core read path end to end:
#   projects → clips → transcript → stories → signed-url
#
# Read-only by default (no rows created, nothing rendered) so it is safe to run
# repeatedly against the shared dev backend. Pass --deep to also fetch a
# story's signed playback URL.
#
# Usage:
#   scripts/dev-api-smoke.sh            # quick read-path check
#   scripts/dev-api-smoke.sh --deep     # also resolve a signed-url
#
# Env: LYRICSYNC_BASE_URL + LYRICSYNC_API_KEY (exported in web sessions).
set -euo pipefail

BASE="${LYRICSYNC_BASE:-${LYRICSYNC_BASE_URL:-}}"
BASE="${BASE%/}"
KEY="${LYRICSYNC_API_KEY:-}"
DEEP=0
[ "${1:-}" = "--deep" ] && DEEP=1

if [ -z "$BASE" ] || [ -z "$KEY" ]; then
  echo "FAIL: LYRICSYNC_BASE_URL and LYRICSYNC_API_KEY must be set" >&2
  exit 2
fi

auth=(-H "Authorization: Bearer $KEY")
pass() { echo "  ✅ $*"; }
fail() { echo "  ‼️  $*" >&2; exit 1; }

# jq is convenient but not guaranteed; fall back to python for JSON poking.
jget() { # jget '<filter>'  — reads JSON from stdin, prints value
  if command -v jq >/dev/null 2>&1; then jq -r "$1"
  else python3 -c "import sys,json; d=json.load(sys.stdin); print($2)"; fi
}

echo "Dev API: $BASE"

# 1. Auth + list projects --------------------------------------------------
echo "[1] GET /api/projects"
projects="$(curl -sS -m 25 "${auth[@]}" "$BASE/api/projects")"
n="$(printf '%s' "$projects" | jget 'length' 'len(d)')"
[ "$n" -ge 0 ] 2>/dev/null || fail "could not parse project list"
pass "$n project(s) visible (auth works)"
[ "$n" -gt 0 ] || { pass "no projects to walk — stopping here"; exit 0; }

pid="$(printf '%s' "$projects" | jget '.[0].id' 'd[0]["id"]')"
pname="$(printf '%s' "$projects" | jget '.[0].name' 'd[0]["name"]')"
echo "    using project: $pname ($pid)"

# 2. Clips -----------------------------------------------------------------
echo "[2] GET /api/projects/$pid/clips"
clips="$(curl -sS -m 25 "${auth[@]}" "$BASE/api/projects/$pid/clips")"
cn="$(printf '%s' "$clips" | jget 'length' 'len(d)')"
pass "$cn clip(s)"

# 3. Transcript ------------------------------------------------------------
echo "[3] GET /api/projects/$pid/transcript"
code="$(curl -sS -m 30 -o /dev/null -w '%{http_code}' "${auth[@]}" "$BASE/api/projects/$pid/transcript")"
case "$code" in
  200) pass "transcript available" ;;
  404|409) pass "no transcript yet (HTTP $code) — fine for an untranscribed project" ;;
  *) fail "transcript returned HTTP $code" ;;
esac

# 4. Stories ---------------------------------------------------------------
echo "[4] GET /api/projects/$pid/stories"
stories="$(curl -sS -m 25 "${auth[@]}" "$BASE/api/projects/$pid/stories")"
sn="$(printf '%s' "$stories" | jget 'length' 'len(d)')"
pass "$sn stor(y/ies)"

# 5. Signed URL (optional, --deep) ----------------------------------------
if [ "$DEEP" = 1 ] && [ "${sn:-0}" -gt 0 ]; then
  sid="$(printf '%s' "$stories" | jget '.[0].id' 'd[0]["id"]')"
  echo "[5] GET /api/stories/$sid/signed-url"
  code="$(curl -sS -m 25 -o /dev/null -w '%{http_code}' "${auth[@]}" "$BASE/api/stories/$sid/signed-url")"
  case "$code" in
    200) pass "signed-url resolved (render is done)" ;;
    409) pass "story not rendered yet (HTTP 409) — expected for in-progress stories" ;;
    *) fail "signed-url returned HTTP $code" ;;
  esac
fi

echo "✅ dev API smoke passed — read path reachable end to end."
