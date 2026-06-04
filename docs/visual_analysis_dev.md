# Visual Analysis — dev test harness (VIS-01)

A scriptable loop to try Gemini visual analysis on real clips, compare a few
approaches, and render crops to eyeball — **before** wiring vision into story
generation. Everything is API-key callable (`Authorization: Bearer lsk_...`).

## What got added
- **Modal** `analyze_visuals` endpoint + `_analyze_worker` (`modal/app.py`) —
  sends a clip to Gemini, returns a timestamped visual track.
- **`modal/visual.py`** — pure prompt-builder + response parser (unit-tested in
  `tests/test_visual_parse.py`).
- **DB** `visual_analyses` table (`db/migrations/20260604_add_visual_analyses.sql`)
  — one row per (clip, variant) run, with a verbose `debug` blob.
- **API** `POST /api/clips/[id]/analyze` and `GET /api/clips/[id]/visual`.

## Variants (different approaches to A/B)
Pass `{"variant": "..."}` to the analyze endpoint. Each call is a separate run,
so you can fire several on the same clip and diff them.

| variant | model | notes |
|---|---|---|
| `flash` (default) | gemini-2.5-flash | cheap/fast; descriptions + highlight beats |
| `flash_lowres` | gemini-2.5-flash | low media resolution (~3× cheaper tokens) — how much detail is lost? |
| `pro` | gemini-2.5-pro | quality ceiling |
| `editorial` | gemini-2.5-flash | also returns ready-to-render `suggested_clips` |

Add more in `VISUAL_VARIANTS` in `modal/app.py`.

## One-time setup
1. **DB:** apply `db/migrations/20260604_add_visual_analyses.sql` (Supabase SQL editor or MCP).
2. **Secret:** add `GEMINI_API_KEY` to the Modal secret `lyricsync-secrets`.
3. **Deploy:** `modal deploy modal/app.py`, copy the `analyze_visuals` URL it prints, set it as `MODAL_ANALYZE_URL` in Vercel.

## The loop
```bash
BASE=https://<your-app>        # or http://localhost:3000
KEY=lsk_...                     # personal API key (Settings → API keys)
PROJ=<project-uuid>

# 0. Upload clips via the web UI (existing flow). Then list clip IDs:
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/projects/$PROJ" | jq '.clips'
CLIP=<clip-uuid>

# 1. Analyze — fire a few variants on the same clip
for v in flash flash_lowres pro editorial; do
  curl -s -XPOST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
    "$BASE/api/clips/$CLIP/analyze" -d "{\"variant\":\"$v\"}"
done

# 2. Inspect — every run with FULL diagnostics (prompt, raw response, token
#    usage, timings, Gemini file state, traceback on error)
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/clips/$CLIP/visual" | jq

#    Just the parsed results, no debug blob:
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/clips/$CLIP/visual?debug=0" | jq '.analyses[] | {variant, result}'

# 3. Crop — turn picked moments into a render (clip-local seconds, by filename).
#    e.g. grab a highlight ±a couple seconds, or an editorial suggested_clip.
curl -s -XPOST -H "Authorization: Bearer $KEY" -H 'Content-Type: application/json' \
  "$BASE/api/projects/$PROJ/stories" \
  -d '{"ranges":[{"source":"IMG_2415.mov","start":10.5,"end":16.0}]}'
STORY=<story-uuid-from-response>

# 4. Download / play
curl -s -H "Authorization: Bearer $KEY" "$BASE/api/stories/$STORY/signed-url" | jq
```

## Notes
- Visual analysis is **independent of transcription** — analyze right after
  upload; no need to wait for the Whisper/align pipeline.
- Gemini timestamps are seconds from clip start, **clip-local** — the same
  timebase the render endpoint trims on, so highlight/segment times drop
  straight into `ranges`.
- `debug.usage` carries the token counts so you can see real per-variant cost.
- Re-running a variant just adds another row; `GET .../visual` is newest-first.
