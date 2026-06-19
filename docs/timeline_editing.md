# Timeline editing (EDL-01)

The editable timeline replaces `ranges_json` as the representation a client
(human or LLM) manipulates between renders. Quote resolution still produces
the *initial* cut; after that, edits go through explicit operations with
validation, revision history, and undo.

Implementation map:

- `modal/timeline.py` — schema, validation, edit ops, timeline → ffmpeg
  compiler (pure stdlib, tested in `tests/test_timeline.py`)
- `modal/app.py` → `edit_timeline` — synchronous Modal endpoint that applies
  ops and bumps the revision; `_render_worker` renders from the timeline
- `web/src/app/api/stories/[id]/{edit,timeline,revisions}` — authenticated API
- `db/migrations/20260610_add_story_timeline.sql` — `stories.timeline_json`,
  `stories.timeline_revision`, `story_revisions`

## Lifecycle

1. Story generation resolves Claude's quotes to `ranges_json` (unchanged).
2. The first call to `POST /api/stories/{id}/edit` (even with `ops: []`)
   materializes `timeline_json` from `ranges_json` — the legacy ±0.08 s trim
   padding is baked into the item boundaries at this point, so the timeline is
   WYSIWYG.
3. Every edit creates revision N+1 and snapshots the full timeline into
   `story_revisions`.
4. `POST /api/stories/{id}/render` renders `timeline_json` when present
   (falling back to `ranges_json` for never-edited stories). Passing `ranges`
   in the render body resets the timeline — it re-materializes from the new
   ranges on the next edit.

## Timeline schema (version 1)

```json
{
  "version": 1,
  "width": 1080, "height": 1920, "fps": 30,
  "tracks": [
    {"type": "video", "items": [
      {"id": "v1", "kind": "clip", "source": "IMG_2415.mov",
       "src_start": 12.40, "src_end": 18.20, "speed": 1.0,
       "transition_in": null, "note": "transcript text for this span"},
      {"id": "v2", "kind": "blank", "duration": 1.0,
       "transition_in": {"type": "crossfade", "duration": 0.3}}
    ]},
    {"type": "text", "items": [
      {"id": "t1", "text": "Title card", "start": 0.0, "end": 3.0,
       "size": 64, "position": "center", "wrap": 22}
    ]}
  ]
}
```

- Video items play in array order. `src_start`/`src_end` are seconds in the
  source clip; `speed` (0.25–4) changes playback rate, so an item's output
  duration is `(src_end - src_start) / speed`.
- `transition_in` joins an item to the **previous** one: `null` is a hard cut,
  `{"type": "crossfade", "duration": s}` overlaps the two (total duration
  shrinks by `s`). The crossfade must be shorter than both adjacent items,
  ≤ 3 s, and the first item's `transition_in` must be null.
- Text items live in **output time** (seconds into the rendered video), so
  they stay put when the video track is re-cut underneath them. `position` is
  `center | upper | lower`; `wrap` is max characters per line.
- `note` is informational (the transcript text the item came from) and has no
  effect on rendering.

## Edit operations

`POST /api/stories/{id}/edit` with `{"ops": [...], "base_revision": n}`.
Ops apply in order, atomically: if any op fails or the resulting timeline is
invalid, nothing is saved and the error message says which op failed and why.

| Op | Fields | Notes |
|---|---|---|
| `trim` | `id`, `src_start`?, `src_end`?, `start_delta`?, `end_delta`? | Absolute values and/or relative deltas; clips only |
| `split` | `id`, `at` | `at` = seconds into the item's source span; second half gets a new id |
| `move` | `id`, `to_index` | Reorder within the video track |
| `delete` | `id` | Remove a video item |
| `set_speed` | `id`, `speed` | 0.25–20.0; clips only |
| `set_mute` | `id`, `mute` | `true` silences the clip (e.g. silent time-lapse); clips only |
| `set_transition` | `id`, `transition` | `null` or `{"type": "crossfade", "duration": s}` (joins to previous item) |
| `insert_clip` | `source`, `src_start`, `src_end`, `index`?, `speed`? | Appends when `index` omitted |
| `insert_blank` | `duration`, `index`? | Black + silence spacer |
| `add_text` | `text`, `start`, `end`, `size`?, `position`?, `wrap`? | Output-time window |
| `update_text` | `id`, any text fields | Partial update |
| `remove_text` | `id` | |

Example — tighten a cut, speed up the middle, crossfade into the ending, and
add a title:

```json
{
  "base_revision": 3,
  "ops": [
    {"op": "trim", "id": "v1", "end_delta": -0.4},
    {"op": "set_speed", "id": "v2", "speed": 1.5},
    {"op": "set_transition", "id": "v3",
     "transition": {"type": "crossfade", "duration": 0.5}},
    {"op": "add_text", "text": "Part Two", "start": 0.0, "end": 2.0,
     "position": "upper"}
  ]
}
```

Response: `{"revision": 4, "duration_secs": 41.3, "timeline": {...}}`.

## API summary

| Endpoint | Purpose |
|---|---|
| `GET /api/stories/{id}/timeline` | Current timeline + revision (`timeline` is null until first edit) |
| `POST /api/stories/{id}/edit` | Apply ops (or `{"restore_revision": n}` to undo); optional `base_revision` for optimistic concurrency (409 on mismatch) |
| `GET /api/stories/{id}/revisions` | Revision history (ops + timestamps, newest first) |
| `POST /api/stories/{id}/render` | Render (timeline-aware; unchanged URL) |

Errors come back as `{"detail": "..."}` with messages written to be actionable
for an LLM caller, e.g.
`op 1 (trim): trim on v2: span would be empty (3.100–2.900)`.

## Render pipeline

`compile_timeline` turns a timeline into one seeked ffmpeg input per clip item
plus a single `filter_complex`:

- per item: trim → speed (`setpts` / chained `atempo`) → scale/pad to
  1080×1920 → fps normalize
- joins: a single `concat` when all joins are cuts (byte-identical graph to
  the legacy renderer); pairwise `concat`/`xfade`+`acrossfade` folding when
  crossfades are present
- text track: `drawtext` filters applied to the joined stream with
  output-time `enable` windows, then `format=yuv420p`

## One-time setup

1. Apply `db/migrations/20260610_add_story_timeline.sql` (Supabase SQL editor
   or MCP `apply_migration`).
2. Deploy Modal (push to `main` touching `modal/**`); note the new
   `edit_timeline` endpoint URL it prints.
3. Set `MODAL_EDIT_URL` to that URL in the Vercel environment.
