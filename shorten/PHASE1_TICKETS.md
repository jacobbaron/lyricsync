# Phase 1 — Infrastructure + Manual Workflow

**Goal:** End-to-end workflow accessible from the phone, with no LLM.
Upload videos → transcribe → read transcript → pick ranges manually →
render cut → download.

Phase 2 (LLM story generation) slots in after `transcribed` status is
reached — that handoff point is the most important interface to preserve
cleanly here.

---

## Phase 1 Overall Acceptance Criteria

1. Opening the app URL on an iPhone shows a usable, mobile-sized UI
2. Login via magic link email (non-allowlisted emails are blocked)
3. Can create a project and pick multiple videos from the camera roll
4. Each video uploads directly to R2 with a visible progress indicator;
   upload state survives the browser being backgrounded briefly
5. Transcription and alignment kick off automatically after upload
   completes; per-clip progress is visible without refreshing
6. The merged transcript is readable in the UI, with each utterance
   labeled by source clip and approximate timestamp
7. User can specify time ranges manually (source clip, start, end) and
   submit them as a cut
8. The cut renders in the background; status is visible without refreshing
9. The finished video plays inline on the phone
10. Tapping Download saves the video to the phone's Files / Photos

---

## User Testing Script

Run this end-to-end on an iPhone after all tickets are complete.

1. **Auth** — Open the app URL in Safari. Enter your email. Check email,
   tap the magic link. Confirm you land on the home screen logged in.
   Close and reopen Safari — confirm session persists.

2. **Block non-owner** — Open a private tab, try logging in with a
   different email. Confirm access is denied after the magic link.

3. **Create project & upload** — Tap "New Project", give it a name. Tap
   "Add Videos", select 2–3 videos from the camera roll (mix of lengths).
   Confirm per-file upload progress bars appear. Background the app for
   30 seconds, return — confirm uploads completed or are still running
   (not silently failed).

4. **Transcription progress** — After upload, confirm the UI transitions
   to a "Transcribing" state automatically. Watch per-clip status update
   without manually refreshing. Confirm all clips reach "Transcribed".

5. **Transcript** — Tap through to the transcript view. Confirm all clips
   are represented, utterances are labeled with the source clip name, and
   timestamps are shown. Scroll through the full transcript on the phone.

6. **Range picker** — Tap "New Cut". Add 3 ranges from different clips
   using the time inputs. Try entering an invalid range (end before start,
   or time beyond clip duration) — confirm it is rejected. Reorder and
   remove a range. Submit.

7. **Render progress** — Confirm the cut enters "Rendering" status
   immediately. Watch status update without refreshing. Confirm it reaches
   "Done" within a reasonable time.

8. **Preview** — Tap the finished cut. Confirm the video plays inline in
   the browser. Scrub through it.

9. **Download** — Tap Download. Confirm the video saves to Files or Photos
   on the phone. Open it in Photos and confirm it plays correctly.

---

## Ticket Dependency Map

```
P1-01 Scaffold & CI
    └── P1-02 Auth
            └── P1-03 DB Schema
                    ├── P1-04 R2 + Presigned Upload API
                    │       └── P1-05 Upload UI
                    │               └── P1-06 Transcribe Task (Modal)
                    │                       └── P1-07 Align & Merge Task (Modal)
                    │                               ├── P1-08 Status Polling UI
                    │                               └── P1-09 Transcript Viewer
                    │                                       └── P1-10 Range Picker UI
                    │                                               └── P1-11 Render Task (Modal)
                    │                                                       └── P1-12 Preview & Download
                    └── P1-08 (also reads project/clip rows from DB)
```

---

## Tickets

---

### P1-01 — Scaffold & CI

Bootstrap the Next.js app and wire up continuous deployment to Vercel.
This ticket has no functional UI — it's the foundation every other ticket
builds on.

**AC:**
- `npm run dev` starts a Next.js 15 app locally with TypeScript and
  Tailwind configured
- A basic home page renders at `/`
- Pushing to `main` triggers an automatic Vercel deployment
- Environment variable placeholders are documented in `.env.example`
- Vercel project is linked to the GitHub repo; production URL is known
  (needed for Supabase redirect config in P1-02)

---

### P1-02 — Auth: Magic Link, Single-User Allowlist

Wire up Supabase Auth so only your email can access the app. Login must
work cleanly on mobile (no password, no app install required).

**AC:**
- `/login` shows an email input; submitting sends a magic link via Supabase
- Tapping the magic link on the phone completes login and redirects to `/`
- All routes except `/login` are protected by middleware; unauthenticated
  requests redirect to `/login`
- Requests authenticated as any email other than the allowlisted one are
  rejected (403) — enforced in middleware using `ALLOWED_EMAIL` env var
- Session persists across browser closes (Supabase session cookie)
- Supabase redirect URL is configured to the Vercel production URL

---

### P1-03 — Database Schema

Create the Postgres schema in Supabase and apply Row Level Security so
all rows are scoped to the authenticated user.

**AC:**
- Tables exist: `projects`, `clips`, `stories`, `feedback` matching the
  schema in `WEBAPP_PLAN.md`
- RLS is enabled on all tables; policies allow read/write only for the
  row's `owner` matching `auth.email()`
- A `db/schema.sql` file in the repo contains the full schema so it can
  be re-applied to a fresh Supabase project
- Status enum values are documented as comments in the SQL

_Status values to encode:_
- `clips.status`: `uploading | transcribing | aligned | error`
- `projects.status`: `uploading | transcribing | transcribed | rendering | done | error`
- `stories.status`: `rendering | done | error`

---

### P1-04 — R2 Storage + Presigned Upload API

Set up the Cloudflare R2 bucket and an API route that hands the client
presigned PUT URLs so video files upload directly from the phone to R2
without passing through the Vercel server.

**AC:**
- R2 bucket exists and is configured (see setup steps in `WEBAPP_PLAN.md`)
- `POST /api/projects` creates a `projects` row (status `uploading`) and
  returns a project ID
- `POST /api/projects/[id]/clips` accepts a filename + content type,
  creates a `clips` row (status `uploading`), and returns a presigned R2
  PUT URL valid for 1 hour
- The presigned URL grants write access to a scoped key path
  (`projects/<project-id>/clips/<clip-id>/original.<ext>`)
- `PATCH /api/clips/[id]` accepts `{ status: 'uploading_complete' }` so
  the client can signal when its PUT to R2 is done; route updates the DB
  row
- All routes verify the caller's Supabase session and that the resource
  belongs to them

---

### P1-05 — Upload UI

The primary mobile entry point. Create a project, pick videos, watch them
upload. This is the first screen a user interacts with.

**AC:**
- Home screen lists existing projects with their names and statuses
- "New Project" flow: enter a name → land on project page
- "Add Videos" opens the native file picker filtered to video types;
  multiple selection is enabled
- Each selected file gets a presigned URL (P1-04), uploads directly to R2
  via `fetch` PUT, and shows an individual progress bar
- On upload completion the client calls `PATCH /api/clips/[id]` to mark
  the clip done, then triggers transcription (P1-06) for that clip
- If the browser is backgrounded mid-upload and foregrounded again,
  in-progress uploads resume or are clearly marked as interrupted (not
  silently dropped)
- Uploading state persists in the DB — refreshing the page shows clips
  in their current state

---

### P1-06 — Transcribe Task (Modal)

Modal function that downloads a clip from R2, extracts audio with ffmpeg,
calls the OpenAI Whisper API, and writes the transcript JSON back to R2.

This is the first Modal task and establishes the pattern all subsequent
tasks follow: read from R2, do work, write to R2, update DB status.

**AC:**
- Modal app is created (`modal deploy` works from the repo)
- `transcribe_clip(clip_id)` function:
  - Downloads the video from R2
  - Extracts 16 kHz mono WAV with ffmpeg (matches existing `extract.py`)
  - Calls the OpenAI Whisper API with word-level timestamps
  - Writes `transcript.json` to R2 at `projects/<pid>/clips/<cid>/transcript.json`
  - Sets `clips.status = 'transcribing'` on start, `'transcribed_raw'` on
    success, `'error'` on failure (with error message stored)
- `POST /api/clips/[id]/transcribe` Vercel route spawns the Modal function
  asynchronously (fire-and-forget — returns immediately so Vercel's
  function timeout is not a concern) and sets `clips.status = 'transcribing'`
- Modal function is invoked via its web endpoint URL (no Modal Python SDK
  required in the Next.js runtime)
- Whisper 25 MB audio limit: if extracted audio exceeds 25 MB, the task
  sets status `error` with a clear message (chunking deferred to a later
  ticket or phase)

---

### P1-07 — Align & Merge Task (Modal)

After all clips in a project are transcribed, run WhisperX alignment on
each clip and merge into a single global-timeline transcript. This is the
boundary between raw transcription and usable data — Phase 2's LLM task
reads the output of this step.

**AC:**
- `align_and_merge(project_id)` Modal function:
  - Runs only when all clips in the project have status `transcribed_raw`
  - For each clip: runs WhisperX wav2vec2 alignment (reusing `align.py`
    logic), writes `transcript_aligned.json` to R2, sets clip status
    `aligned`
  - Runs `sync.py` + `merge.py` logic across all clips to produce
    `merged.json` on a shared global timeline with `global_start`,
    `global_end`, and `source` per word
  - Writes `merged.json` to `projects/<pid>/merged.json` in R2
  - Sets `projects.status = 'transcribed'` on success
- Trigger: `POST /api/projects/[id]/align` is called automatically by the
  client once all clips reach `transcribed_raw` (client detects this via
  polling in P1-08)
- On failure, sets `projects.status = 'error'` with a message

---

### P1-08 — Status Polling UI

Real-time-feeling progress without websockets. The UI polls the project
status endpoint and updates the display as clips move through the
pipeline.

**AC:**
- `GET /api/projects/[id]` returns the project row plus all clip rows
  with their current statuses
- The project page polls this endpoint every 3 seconds while the project
  status is not terminal (`transcribed`, `done`, or `error`)
- Per-clip status is shown visually (e.g., uploading / transcribing /
  aligned / error with a short message)
- Overall project status is shown (e.g., "Transcribing 2 of 3 clips…")
- When all clips reach `aligned` and the project reaches `transcribed`,
  the UI stops polling and reveals the transcript viewer (P1-09) and
  range picker (P1-10) without requiring a page reload
- Polling stops immediately on error; the error message is displayed

---

### P1-09 — Transcript Viewer

Display the merged transcript so the user can read it and identify
moments to include in their cut. This is the core reference the user
works from when building ranges in P1-10.

**AC:**
- Fetches `merged.json` from R2 (via a signed GET URL from the API)
- Renders utterances grouped by source clip, in global timeline order
- Each utterance shows: source clip name, timestamp (global start, e.g.
  `0:42`), and the text
- Text is readable at mobile font sizes without horizontal scrolling
- Tapping a timestamp copies it to clipboard (convenience for filling in
  the range picker)
- If `merged.json` is not yet available (project not yet `transcribed`),
  the viewer shows a loading state consistent with P1-08's progress UI

---

### P1-10 — Range Picker UI

Let the user specify which segments to include in a cut, using the
transcript as reference. No LLM — this is the manual version of what
Phase 2 automates.

**AC:**
- Accessible from the project page once status is `transcribed`
- User can add a range: select source clip from a dropdown (populated from
  the project's clips), enter start time and end time in `mm:ss` or
  decimal seconds
- Validation: end > start; times are within the clip's duration; at least
  one range required to submit
- User can reorder ranges (drag or up/down buttons) and remove any range
- "Create Cut" button calls `POST /api/projects/[id]/stories` with the
  ranges as JSON, creates a `stories` row (status `rendering`), and
  immediately triggers P1-11
- The UI navigates to the story page after submission
- Source clip names in the dropdown match the `source` values in
  `merged.json` so ranges map correctly to the render task

---

### P1-11 — Render Task (Modal)

Modal function that reads the story's ranges, downloads the relevant clip
segments from R2, and splices them into a final MP4 using the existing
`splice.py` logic.

**AC:**
- `render_story(story_id)` Modal function:
  - Reads story `ranges_json` and `project_id` from DB
  - Downloads only the source clips referenced in the ranges (not all
    clips in the project)
  - Runs `splice.py` ffmpeg logic to cut and concatenate segments
  - Forces 8-bit yuv420p BT.709 output (existing behavior in `cut.py` for
    iPhone HDR compatibility)
  - Writes output to `projects/<pid>/stories/<sid>/output.mp4` in R2
  - Sets `stories.status = 'done'` and stores the R2 key on success;
    `'error'` with message on failure
  - Sets `projects.status = 'done'` if this is the first completed story
- `POST /api/stories/[id]/render` Vercel route spawns the Modal function
  asynchronously; called automatically by P1-10 after story row creation
- Story page polls `GET /api/stories/[id]` every 3 seconds until terminal
  status (mirrors P1-08 pattern)

---

### P1-12 — Preview & Download

The finish line. User watches the cut and saves it to their phone.

**AC:**
- Story page shows a `<video>` element with the R2 output URL once
  `stories.status = 'done'`
- Video plays inline in Safari on iOS without requiring full-screen
- "Download" button is a direct link to the R2 file with
  `Content-Disposition: attachment` set (via a signed URL from the API
  that adds this header, or via a Cloudflare Worker redirect)
- Tapping Download on iOS saves the file to the Files app; user can
  optionally save to Photos from there
- If the story is still rendering, the page shows the render progress
  state from P1-11's polling
- If rendering errored, the error message is shown with a "Retry" button
  that re-triggers P1-11

---

## Interface Contracts Between Tickets

These are the specific data shapes tickets depend on — get these right and
tickets can be built in parallel once dependencies are unblocked.

**R2 key structure** (established in P1-04, read by P1-06/07/11/12):
```
projects/<project-id>/clips/<clip-id>/original.<ext>
projects/<project-id>/clips/<clip-id>/audio.wav
projects/<project-id>/clips/<clip-id>/transcript.json
projects/<project-id>/clips/<clip-id>/transcript_aligned.json
projects/<project-id>/merged.json
projects/<project-id>/stories/<story-id>/output.mp4
```

**`merged.json` shape** (written by P1-07, read by P1-09 and P1-11):
```json
{
  "words": [
    {
      "text": "hello",
      "global_start": 0.42,
      "global_end": 0.81,
      "local_start": 0.42,
      "local_end": 0.81,
      "source": "booth.mov",
      "source_path": "projects/.../clips/.../original.mov"
    }
  ]
}
```
The `source` value in `merged.json` must match the `source` field in
`ranges_json` for P1-11 to resolve clips correctly.

**`ranges_json` shape** (written by P1-10 to DB, read by P1-11):
```json
[
  { "source": "booth.mov", "start": 12.40, "end": 15.46 },
  { "source": "control.mov", "start": 3.20, "end": 6.10 }
]
```

**Status polling response** (`GET /api/projects/[id]`, used by P1-08):
```json
{
  "id": "...",
  "status": "transcribing",
  "clips": [
    { "id": "...", "filename": "booth.mov", "status": "aligned" },
    { "id": "...", "filename": "control.mov", "status": "transcribing" }
  ]
}
```

**Modal invocation pattern** (P1-06, P1-07, P1-11):
All Modal functions are deployed as web endpoints. Vercel API routes call
them via HTTP POST with a JSON body containing the entity ID. Modal
handles auth via a shared secret header (`MODAL_WEBHOOK_SECRET` in both
envs). Functions return `{ "status": "accepted" }` immediately; all work
is async. This keeps Vercel function execution under 1 second.
