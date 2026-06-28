# Cross-project cuts

LyricSync's data model is `projects → clips → stories`, and a story has one
`project_id`. Historically a story could only assemble clips from its own
project: the render worker resolved each timeline clip item's `source`
(a bare filename like `IMG_2427.mov`) against
`clips WHERE project_id = story.project_id`.

Cross-project cuts let a single story/timeline splice clips that live in
**different** projects, so one rendered output can mix footage from your whole
library — without moving or copying clips between projects.

This is shipped in two tiers. **Tier 1 (assembly / trims / overlays) and Tier 2
(transcript-dependent "smart" editing across projects) are both built.**

---

## Why clip_id, not filename

Bare filenames are not globally unique. Every iPhone produces an `IMG_2427.mov`,
so two different projects routinely contain clips with identical filenames.
Resolving a cross-project reference by filename would be ambiguous (and silently
pick the wrong clip). Therefore cross-project references are keyed by the clip's
**`clip_id`** (a uuid, globally unique), never by filename.

The render engine was already project-agnostic at the byte level:
`_render_worker` downloads each clip by its full `r2_key` (globally unique) and
caches by `r2_key`. The only thing scoping a render to one project was *source
resolution* — turning a `source` filename into an `r2_key` within one project.
Tier 1 changes only that resolution step.

---

## Tier 1 — what shipped (this PR)

### Timeline schema

Video-track clip items gain an optional `clip_id` (uuid) field alongside the
existing `source` (filename):

```json
{"id": "v1", "kind": "clip",
 "clip_id": "0b9c…-uuid",        // optional, authoritative when present
 "source": "IMG_2427.mov",       // human-readable label / back-compat
 "src_start": 0.0, "src_end": 3.0, "speed": 1.0, "transition_in": null}
```

- When `clip_id` is present it is the **authoritative** reference; the clip is
  resolved globally (across all projects).
- When `clip_id` is absent the item resolves the old way: `source` filename
  within the story's home `project_id`.
- `source` stays as a human-readable label even on cross-project items, so
  existing tooling and diffs remain legible.

### Render resolution (`modal/app.py` → `_render_worker`)

Source resolution was rewritten to resolve each video item to an `r2_key`:

- items with a `clip_id` → looked up globally via `clips WHERE id IN (…)`,
  regardless of project;
- items without a `clip_id` → looked up by filename within the story's home
  `project_id` (unchanged behavior).

The resolution maps are built only from the clip_ids / filenames the timeline
**actually references** (two narrow `IN (…)` queries), not a blanket
`project_id` scan. The download cache stays keyed by `r2_key` (globally unique),
so a clip shared across cuts is still downloaded once.

Resolution failures give clear errors:
`clip not found for clip_id: '…'` or `source clip not found in project: '…'`.

### Validation (`modal/timeline.py` → `validate_timeline`)

A clip item is now valid if it carries **either** a non-empty `source` filename
**or** a non-empty `clip_id`. A present-but-empty `clip_id` is rejected. This
means a foreign-clip item that only carries a `clip_id` (and a label `source`)
passes validation without the filename having to resolve in any single project
at validation time.

### Compiler contract (`modal/timeline.py` → `compile_timeline`)

`resolve_source` now receives the **whole clip item** (not just the filename),
so the worker can resolve by `clip_id` first and fall back to `source`. This is
the one back-compat-affecting API change, and it is internal to the render
worker (the only production caller). `tests/test_timeline.py` was updated to the
item-based contract.

### Authoring path

A timeline carrying foreign `clip_id`s can be persisted and rendered through the
existing endpoints:

- `POST /api/stories/[id]/edit` — the `insert_clip` op now accepts an optional
  `clip_id` (and an optional `source` label), so a cross-project timeline can be
  built op-by-op through the REST API.
- `POST /api/projects/[id]/stories` and `POST /api/stories/[id]/render` —
  unchanged; the home `project_id` is just where the story row is stored.

Trim (`trim`, `split`) and **drawtext overlays** (text-track `add_text` items,
which live in output time) work on foreign clips exactly as on local ones.

### What works cross-project in Tier 1

- **Assembly**: concatenating / ordering clips from multiple projects in one
  cut.
- **Per-clip knobs**: `speed`, `mute`, `audio_fx`, crossfade transitions.
- **Trims**: `trim` / `split` on foreign clips.
- **drawtext overlays**: text title cards over foreign clips (output-time,
  transcript-independent).
- **Canvas auto-fit** and all byte-level render behavior (cache by `r2_key`).

### Back-compatibility guarantee

Existing single-project timelines (no `clip_id`, bare filenames) render exactly
as before. `clip_id` is purely additive; absence of it is the legacy path. The
only changed internal contract is `resolve_source(item)` vs the old
`resolve_source(filename)`, both exercised by the render worker and the unit
tests.

---

## Tier 2 — what shipped

Tier 2 makes the **transcript-dependent / "smart"** editing features work across
projects. Previously these resolved a clip's word timings and audio analysis by
`(home project_id, filename)`, which missed for a foreign clip — so Tier 1
**blocked** `clean_speech` on a `clip_id` item with a clear error. Tier 2 routes
those lookups through a **per-clip resolver keyed by `clip_id`**, so editing a
foreign clip behaves identically to a local one.

### Final design

Transcript/audio lookups are resolved **per referenced clip item**, keyed by
`clip_id` when present (cross-project) and falling back to the home-project
filename otherwise (legacy/local):

- **Words** (`_load_words_by_clip_id` in `app.py`): for each `clean_speech`
  target that carries a `clip_id`, resolve the clip's owning `project_id` from a
  single global `clips` lookup, read THAT project's `merged.json` once, and keep
  only that clip's words (sliced by the clip's own filename within its own
  project). Returns `{clip_id: [words…]}`.
- **Audio** (`_load_audio_by_clip_id` in `app.py`): resolve each target's
  `clip_id → (project_id, clip_id)` globally and read its per-clip
  `projects/<pid>/clips/<cid>/audio_analysis.json` directly (the path was always
  per-clip; only the lookup was home-scoped). Returns
  `{clip_id: {vad_prob, vad_hop, peaks, peaks_hop}}`. Shared read/shape logic
  lives in `_read_clip_audio_analysis`, used by both the filename and clip_id
  paths.
- **Threading** (`timeline.py`): `apply_ops` and `expand_clean_speech` gained
  `words_by_clip_id` / `audio_by_clip_id` params. The pure helper
  `_words_for_item` picks a clip item's words: `words_by_clip_id[clip_id]` for a
  foreign clip (used verbatim — NOT re-filtered by the label `source`), else the
  flat home `words` filtered by filename. `_audio_for_item` does the analogous
  pick for audio. The local default (flat words / `audio_by_source`) is
  unchanged.

**Filename-collision safety.** Two clips in different projects can share a
filename (every iPhone makes an `IMG_2427.mov`). Tier 2 never globs the home
project's flat word list by filename for a foreign clip — it resolves words
per `clip_id` from the clip's own project, so a foreign clip can't pick up a
home-project clip's words just because the filenames match. This is covered by a
dedicated collision test.

**Call sites wired:** `edit_timeline` and `preview_clean_speech` (both in
`app.py`) now gather the `clip_id`s their `clean_speech` ops reference and pass
the clip_id-keyed words/audio maps through. `clean_speech` is expanded and
persisted at **edit time** (stored into `timeline_json`), so the render worker
needs no transcript lookup — it stays purely byte-level (already cross-project
since Tier 1).

**Back-compatibility.** Same-project `clean_speech` (items with a bare filename,
no `clip_id`) takes exactly the legacy path and produces identical cuts; the
clip_id maps are additive and ignored for local items. The only guard left in
`apply_ops` is the "no transcript loaded at all" error (neither flat words nor a
clip_id map) — the Tier 1 "cross-project not supported" guard is removed.

### What was affected (now working)

1. **`clean_speech`** (`modal/timeline.py` `apply_ops` →
   `expand_clean_speech`) — needs the targeted clip's aligned word timings and
   per-clip audio analysis (VAD curve + waveform peaks) to plan the silence /
   filler jump-cuts. Now resolved per `clip_id` (see Final design above) so a
   foreign clip's words/audio come from its own owning project.

2. **Word-aligned overlays** — any future feature that auto-places overlay copy
   from a clip's transcript words shares the same per-clip resolver, so it
   inherits cross-project support for free. (Plain `add_text` drawtext overlays
   never needed it — they're output-time and were cross-project from Tier 1.)

### Product decisions (settled)

- **Single-user.** No permissions / sharing work is needed; all clips belong to
  the same owner, so a global `clip_id` lookup is safe.
- **Full editing parity is the goal.** Tier 2 aims to make every edit op behave
  identically whether a clip is local or foreign.
- **Library-wide search / discovery is out of scope for both tiers.** Finding
  *which* clip to splice (cross-project search) is a later phase; both tiers
  assume the caller already has the `clip_id`s.

---

## Implementation map

**Tier 1**
- `modal/timeline.py` — `clip_id` in the schema docstring, `validate_timeline`
  (accept `clip_id`), `compile_timeline` (`resolve_source(item)` contract),
  `_op_insert_clip` (`clip_id` passthrough).
- `modal/app.py` → `_render_worker` — global `clip_id` resolution +
  home-project filename fallback; per-item `_resolve_r2_key`.

**Tier 2**
- `modal/timeline.py` — `_words_for_item` / `_audio_for_item` per-clip pickers;
  `apply_ops` and `expand_clean_speech` gain `words_by_clip_id` /
  `audio_by_clip_id`; the Tier 1 cross-project `clean_speech` guard removed (the
  "no transcript loaded" guard kept).
- `modal/app.py` — `_load_words_by_clip_id`, `_load_audio_by_clip_id`,
  `_resolve_clips_global`, `_read_clip_audio_analysis`, `_clean_speech_targets`;
  `_load_audio_by_source` scoped to local (no-`clip_id`) targets; `edit_timeline`
  and `preview_clean_speech` wired to pass the clip_id-keyed maps.
- `tests/test_timeline.py` (`_words_for_item` incl. filename collision),
  `tests/test_speech_cleanup.py` (foreign-clip words + VAD audio by clip_id,
  filename-collision cut, same-project back-compat unchanged).
