# Cross-project cuts

LyricSync's data model is `projects → clips → stories`, and a story has one
`project_id`. Historically a story could only assemble clips from its own
project: the render worker resolved each timeline clip item's `source`
(a bare filename like `IMG_2427.mov`) against
`clips WHERE project_id = story.project_id`.

Cross-project cuts let a single story/timeline splice clips that live in
**different** projects, so one rendered output can mix footage from your whole
library — without moving or copying clips between projects.

This is shipped in two tiers. **Tier 1 (this doc's first half) is built.**
Tier 2 (transcript-dependent "smart" editing across projects) is planned.

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

## Tier 2 — plan (NOT built here)

Tier 2 makes the **transcript-dependent / "smart"** editing features work across
projects. Today these resolve a clip's word timings and audio analysis by
`(home project_id, filename)`, which misses for a foreign clip.

### What's affected

1. **`clean_speech`** (`modal/timeline.py` `apply_ops` →
   `expand_clean_speech`). It needs the targeted clip's aligned word timings and
   per-clip audio analysis (VAD curve + waveform peaks) to plan the silence /
   filler jump-cuts.
   - `_load_words(project_id)` (`app.py:~132`) reads the **home** project's
     `projects/<pid>/merged.json`. A foreign clip's words aren't there.
   - `_load_audio_by_source(project_id, …)` (`app.py:~149`) maps a clip filename
     → `id` within the **home** project, then reads
     `projects/<pid>/clips/<cid>/audio_analysis.json`. A foreign clip's filename
     doesn't match a home-project clip.

   **Tier 1 behavior:** `clean_speech` on an item that carries a `clip_id` is
   **blocked with a clear error** (rather than silently producing a no-op cut).
   The render-time path is unaffected because `clean_speech` is expanded at edit
   time and stored in `timeline_json`.

2. **Word-aligned overlays** — any future feature that auto-places overlay copy
   from a clip's transcript words has the same `(project_id, filename)` lookup
   problem. (Plain `add_text` drawtext overlays do **not** — they're output-time
   and already cross-project.)

### Proposed implementation

Route the transcript/audio lookups through a **per-clip resolver keyed by
`clip_id`** instead of `(home project_id, filename)`:

- **Words**: resolve the foreign clip's owning `project_id` from its `clip_id`,
  then read that project's `merged.json` and filter by `source` (filename). A
  cleaner long-term option is a **per-clip words file**
  (e.g. `projects/<pid>/clips/<cid>/words.json`) so a cross-project edit reads
  exactly one clip's words without loading another project's whole transcript.
- **Audio analysis**: already per-clip at
  `projects/<pid>/clips/<cid>/audio_analysis.json`. Only the **lookup** needs to
  go global: resolve `clip_id → (project_id, clip_id)` and read that path
  directly, instead of mapping filename→id within the home project.
- Generalize `_load_words` / `_load_audio_by_source` (and the render-time
  `clean_speech` expansion path) to accept a set of `clip_id`s and return data
  keyed by `clip_id`, removing the implicit home-project assumption.

### Product decisions already made

- **Single-user.** No permissions / sharing work is needed; all clips belong to
  the same owner, so a global `clip_id` lookup is safe.
- **Full editing parity is the goal.** Tier 2 aims to make every edit op behave
  identically whether a clip is local or foreign.
- **Library-wide search / discovery is out of scope for both tiers.** Finding
  *which* clip to splice (cross-project search) is a later phase; both tiers
  assume the caller already has the `clip_id`s.

---

## Implementation map

- `modal/timeline.py` — `clip_id` in the schema docstring, `validate_timeline`
  (accept `clip_id`), `compile_timeline` (`resolve_source(item)` contract),
  `_op_insert_clip` (`clip_id` passthrough), `apply_ops` (Tier 2 `clean_speech`
  guard).
- `modal/app.py` → `_render_worker` — global `clip_id` resolution +
  home-project filename fallback; per-item `_resolve_r2_key`.
- `tests/test_timeline.py`, `tests/test_speech_cleanup.py` — item-based resolver
  contract, `clip_id` validation/compile, cross-project `clean_speech` guard.
