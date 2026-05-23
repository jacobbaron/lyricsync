# shorten — Web App Productionization Plan

Turn the `shorten/` POC into a mobile-accessible, single-user web app:
upload phone videos → auto-transcribe → LLM picks story options → user
selects and iterates → download the final cut.

---

## What We're Building

```
Phone browser
    │
    │  upload videos from camera roll
    ▼
[Web UI — PWA]
    │
    │  presigned URL, direct upload
    ▼
[Cloud Storage]  ←──────────────────────────────┐
    │                                            │
    │  async job                                 │ read/write files
    ▼                                            │
[Heavy Compute — Modal]                          │
    ├── transcribe per clip (Whisper)            │
    ├── align timings (WhisperX / wav2vec2)      │
    ├── merge into global timeline               │
    ├── generate stories (Claude API)            │
    └── render final cut (ffmpeg)  ──────────────┘
    │
    │  job status updates
    ▼
[Request Handler — tiny always-on API]
    │
    │  reads/writes
    ▼
[Database — Postgres]
```

The request handler is the only thing that runs 24/7. Everything else
spins up on demand and costs nothing when idle.

---

## Core User Stories

### 1 — Upload
User opens the PWA on their phone, creates a project, picks N videos
from their camera roll. Videos upload directly to cloud storage (not
through the server). Progress shown per file. Page survives backgrounding
— upload state persists in the DB.

### 2 — Transcribe & Align
App kicks off a background job: extract audio from each clip, transcribe
with Whisper (word-level timestamps), re-align with wav2vec2 for
phoneme-tight timing. Progress visible in the UI per clip.

### 3 — Review Transcript
User can read the merged transcript (all clips on one global timeline,
source clip labeled per utterance) while the LLM is thinking.

### 4 — Pick a Story
LLM returns 3 story options. Each shows:
- Title + one-paragraph description of what the story is about
- The transcript excerpts it uses (with source clip labels)
- Estimated output duration

User taps one to queue it for rendering.

### 5 — Preview the Cut
Render runs in the background. When done, video streams inline from
cloud storage. User watches on their phone.

### 6 — Iterate with Feedback
Text box beneath the preview: *"tighter ending", "drop the bit about X",
"start with the laugh"*. The app sends the current story (ranges +
transcript context) plus the feedback back to the LLM. New story
generated, re-rendered.

### 7 — Download
Tap Download → video saves to phone Files / Photos via a direct storage
URL (`Content-Disposition: attachment`).

---

## Architecture Decisions

Below are the meaningful choices with tradeoffs. Each is a **decision
point** — pick one per section before starting implementation.

---

### A. Heavy Compute Platform

Where Whisper, wav2vec2 alignment, and ffmpeg rendering run.

| Option | Pros | Cons |
|---|---|---|
| **Modal** *(recommended)* | Python-native (wrap existing scripts almost verbatim), GPU support, true scale-to-zero, per-second billing, large timeout support | Vendor lock-in on task decorator pattern |
| **Google Cloud Run** | Docker-based (existing Dockerfile reusable), scales to zero, 60-min timeout | GPU support limited, more config overhead |
| **AWS Lambda + ECS** | Mature ecosystem | Lambda 15-min limit too short for long transcriptions; ECS always-on |
| **Fly.io Machines** | Start/stop on demand, simple | Manual orchestration; no native GPU |

**Recommendation: Modal.** The existing Python scripts become Modal
functions with a decorator. GPU available for WhisperX when speed
matters. Billing is per second with no idle cost.

---

### B. Request Handler / API

The always-on piece. Must be cheap (this is the one persistent cost).

| Option | Pros | Cons |
|---|---|---|
| **Vercel serverless functions** *(recommended)* | Free for personal use, Next.js API routes co-located with UI, zero ops | Cold starts on infrequent use (fine for personal) |
| **Fly.io shared-cpu-1x** | Always warm, ~$4/mo, full control | Small ongoing cost |
| **Supabase Edge Functions** | Integrated with Supabase auth/DB | Limited runtime, Deno not Python |

**Recommendation: Vercel.** Next.js gives you API routes and the UI in
one deployment. Free tier is more than enough for one user. Cold starts
on a personal app are acceptable.

---

### C. Database

| Option | Pros | Cons |
|---|---|---|
| **Supabase Postgres** *(recommended)* | Free tier (500 MB), built-in auth, Row Level Security, real-time if needed, S3-compatible storage bucket included | Supabase pauses free projects after 1 week inactivity (upgrade to Pro $25/mo to avoid, or ping it on a cron) |
| **PlanetScale** | Free tier, MySQL | Not Postgres; auth is separate |
| **Neon** | Serverless Postgres, scales to zero | Auth is separate |
| **Fly.io Postgres** | Full control | You manage backups |

**Recommendation: Supabase.** Auth + Postgres + file storage in one
place. If the free-tier pause becomes annoying, upgrade to Pro or move to
Neon + a separate auth service.

---

### D. File Storage

Videos in, rendered outputs out.

| Option | Pros | Cons |
|---|---|---|
| **Cloudflare R2** *(recommended)* | Zero egress fees (huge for video), S3-compatible API, generous free tier (10 GB) | No built-in CDN (though Cloudflare Workers can front it) |
| **AWS S3** | Industry standard, great tooling | Egress fees add up (~$0.09/GB out) |
| **Supabase Storage** | Integrated with auth/DB | 1 GB free, then $0.021/GB; egress not free |
| **Backblaze B2** | Cheap ($0.006/GB), free egress to Cloudflare | Less mainstream tooling |

**Recommendation: Cloudflare R2.** No egress fees is the right answer for
video. Use presigned PUT URLs so the phone uploads directly to R2 without
touching your API.

---

### E. Transcription

| Option | Pros | Cons |
|---|---|---|
| **OpenAI Whisper API** *(recommended for MVP)* | Simple, no GPU needed, $0.006/min | Word timestamps are loose (±200-500ms), 25 MB upload limit |
| **WhisperX on Modal GPU** | Phoneme-tight timestamps, no size limit | More setup, GPU cost (~$0.001–0.003/min) |
| **Groq Whisper API** | Very fast, cheap | Word timestamps less reliable |

**Recommendation: Start with OpenAI Whisper API.** Already used in the
POC. Follow with the existing `align.py` step (WhisperX on Modal) to
tighten timestamps before rendering. The two-step approach matches what
the POC does today.

---

### F. LLM for Story Generation

| Option | Pros | Cons |
|---|---|---|
| **Claude (Anthropic API)** *(recommended)* | Large context window for long transcripts, structured tool use for JSON output, strong instruction following | Paid |
| **GPT-4o** | Also strong, you may already have a key | Slightly weaker on long structured output |
| **Gemini 1.5 Pro** | Very long context window | Less predictable structured output |

**Recommendation: Claude.** The existing `critique.py` uses GPT-4o; the
story-generation task is better suited to Claude's instruction following
and JSON tool use. Use `claude-opus-4-7` for story generation,
`claude-haiku-4-5` for cheaper iteration passes.

---

### G. Auth

Single-user app, so this can be simple.

| Option | Pros | Cons |
|---|---|---|
| **Supabase Auth + email allowlist** *(recommended)* | Built-in with the DB, magic-link login works on phone (no password), allowlist by email | Tied to Supabase |
| **Clerk** | Polished UI, easy setup | Another vendor, free tier has limits |
| **NextAuth.js (Google OAuth)** | Standard, works with Vercel | More config |
| **HTTP Basic Auth** | Zero setup | Not great on mobile |

**Recommendation: Supabase Auth with magic-link login.** You enter your
email, get a link on your phone, tap it — logged in. No password to
remember. Allowlist enforced by a Supabase RLS policy or middleware check
against your email.

---

## Data Model

```sql
projects (
  id          uuid primary key,
  owner       text,              -- your email
  name        text,
  status      text,              -- see state machine below
  created_at  timestamptz
)

clips (
  id             uuid primary key,
  project_id     uuid references projects,
  r2_key         text,           -- path in R2
  filename       text,
  duration_secs  float,
  global_start   float,          -- offset on shared timeline
  transcript_r2_key text,        -- merged transcript JSON
  status         text            -- uploading | transcribing | done | error
)

stories (
  id           uuid primary key,
  project_id   uuid references projects,
  title        text,
  description  text,
  ranges_json  jsonb,            -- [{source, start, end}, ...]
  render_r2_key text,            -- output video in R2
  status       text,             -- generating | rendering | done | error
  created_at   timestamptz
)

feedback (
  id         uuid primary key,
  story_id   uuid references stories,
  text       text,
  created_at timestamptz
)
```

**Project status state machine:**
```
uploading → transcribing → transcribed → generating_stories
    → stories_ready → [user picks one] → rendering → done
```
Feedback re-enters at `generating_stories` with the previous story +
feedback text as additional context.

---

## Modal Function Map

Each existing POC script becomes one Modal function:

| POC script | Modal function | Input | Output |
|---|---|---|---|
| `transcribe.py` | `transcribe_clip(r2_key)` | R2 path to video | transcript JSON written to R2 |
| `sync.py` + `merge.py` | `merge_transcripts(project_id)` | project ID | merged transcript JSON in R2; updates `clips.global_start` |
| (new) | `generate_stories(project_id)` | merged transcript | 3 story objects written to DB |
| `splice.py` | `render_story(story_id)` | story ID (reads ranges from DB) | output MP4 written to R2 |

All functions update the relevant DB row's `status` field when they start
and finish (or error). The UI polls `/api/projects/:id` every 3 seconds.

---

## LLM Story Generation Prompt (sketch)

```
You are a video editor. Given a merged transcript from multiple camera
angles of the same session, identify 3 distinct, compelling short-form
stories that can be cut from this footage.

For each story output:
- title: short descriptive title
- description: 2-3 sentences on what makes this story work
- ranges: ordered list of {source, start_sec, end_sec} segments to include
- estimated_duration_secs: sum of range durations

Rules:
- Each story should be 60–180 seconds of output
- Cuts should land at sentence boundaries (use the transcript)
- Prefer complete thoughts over partial sentences
- You may use any clip as a source for any range

Transcript (JSON with global timestamps and source labels):
<transcript>
```

Feedback iteration appends to the same conversation thread with:
```
The user watched the story and has this feedback: "<feedback text>"
Adjust the ranges accordingly and return the revised story in the same
JSON format.
```

---

## Human Setup Steps

Everything below requires action from you before any code is deployed.

### Step 1 — Accounts to Create

Work through these in order (some later steps depend on earlier ones).

| Service | URL | Purpose | Decision point |
|---|---|---|---|
| **Vercel** | vercel.com | Host the web app and API | See §B above |
| **Supabase** | supabase.com | Postgres DB + Auth | See §C and §G above |
| **Cloudflare** | cloudflare.com | R2 storage (free egress for video) | See §D above |
| **Modal** | modal.com | Heavy compute (transcription, render) | See §A above |
| **Anthropic** | console.anthropic.com | Claude API for story generation | See §F above |
| **OpenAI** | platform.openai.com | Whisper API for transcription | See §E above |

You may already have OpenAI and Anthropic accounts.

---

### Step 2 — Cloudflare R2 Setup

1. In Cloudflare dashboard → **R2** → Create bucket, name it e.g. `lyricsync-media`
2. Under bucket settings, enable **public access** for reading (so presigned
   read URLs work) OR keep private and use presigned GET URLs — either works
3. Go to **My Profile → API Tokens → Create Token** → use the
   "Edit Cloudflare Workers" template, scope it to your R2 bucket
4. Note down:
   - `CLOUDFLARE_ACCOUNT_ID`
   - `CLOUDFLARE_R2_ACCESS_KEY_ID`
   - `CLOUDFLARE_R2_SECRET_ACCESS_KEY`
   - `R2_BUCKET_NAME`
   - `R2_PUBLIC_URL` (your bucket's public domain, like `pub-xxx.r2.dev`)

---

### Step 3 — Supabase Setup

1. Create a new project, pick a region close to you
2. In **Authentication → Providers**, enable **Email** provider and
   turn on **Magic Link** (disable email/password sign-in)
3. In **Authentication → URL Configuration**, add your Vercel URL as a
   redirect URL (you'll know this after Step 5 — come back)
4. In **Authentication → Users**, manually add your email address
5. Run the SQL in `db/schema.sql` (to be created) in the Supabase SQL editor
6. Note down:
   - `SUPABASE_URL` (from Project Settings → API)
   - `SUPABASE_ANON_KEY` (public, safe to use in browser)
   - `SUPABASE_SERVICE_ROLE_KEY` (private, server-side only — never expose)

---

### Step 4 — Modal Setup

1. `pip install modal` then `modal token new` — this opens a browser, logs
   you into modal.com, and writes a token to `~/.modal.toml`
2. In the Modal dashboard, create a **Secret** named `lyricsync-secrets`
   with these keys (they'll be injected into Modal functions):
   ```
   OPENAI_API_KEY=...
   ANTHROPIC_API_KEY=...
   CLOUDFLARE_ACCOUNT_ID=...
   CLOUDFLARE_R2_ACCESS_KEY_ID=...
   CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
   R2_BUCKET_NAME=...
   SUPABASE_URL=...
   SUPABASE_SERVICE_ROLE_KEY=...
   ```
3. The Modal functions will be deployed separately from the web app:
   `modal deploy src/modal_tasks.py`

---

### Step 5 — Vercel Setup

1. Push this repo to GitHub (it's already configured for this)
2. In Vercel dashboard → **Add New Project** → import from GitHub
3. Set **Framework Preset** to Next.js
4. Under **Environment Variables**, add:
   ```
   SUPABASE_URL=...
   SUPABASE_ANON_KEY=...
   SUPABASE_SERVICE_ROLE_KEY=...
   CLOUDFLARE_ACCOUNT_ID=...
   CLOUDFLARE_R2_ACCESS_KEY_ID=...
   CLOUDFLARE_R2_SECRET_ACCESS_KEY=...
   R2_BUCKET_NAME=...
   R2_PUBLIC_URL=...
   MODAL_TOKEN_ID=...      # from ~/.modal.toml
   MODAL_TOKEN_SECRET=...  # from ~/.modal.toml
   ALLOWED_EMAIL=jacobbaron@gmail.com
   ```
5. Deploy. Note the production URL (e.g. `https://lyricsync.vercel.app`)
6. Go back to Supabase → Auth → URL Configuration and add the Vercel URL

---

### Step 6 — Install on Phone

1. Open the Vercel URL in Safari (iOS) or Chrome (Android)
2. iOS: tap the Share icon → **Add to Home Screen** → Add
3. Android: tap the browser menu → **Install app** or **Add to Home Screen**

The app icon will appear on your home screen and open full-screen without
browser chrome.

---

## Secret Injection Summary

| Secret | Where it lives | Who reads it |
|---|---|---|
| `SUPABASE_URL` | Vercel env + Modal secret | API routes + Modal tasks |
| `SUPABASE_ANON_KEY` | Vercel env (public) | Browser (via Next.js) |
| `SUPABASE_SERVICE_ROLE_KEY` | Vercel env + Modal secret | API routes + Modal tasks only |
| `CLOUDFLARE_*` | Vercel env + Modal secret | API routes (presign) + Modal tasks (write) |
| `OPENAI_API_KEY` | Modal secret | Modal transcribe task |
| `ANTHROPIC_API_KEY` | Modal secret | Modal story-gen task |
| `MODAL_TOKEN_ID/SECRET` | Vercel env | API routes (to invoke Modal) |
| `ALLOWED_EMAIL` | Vercel env | Middleware auth check |

Never put `SERVICE_ROLE_KEY` or any Modal/cloud credentials in
client-side code. Supabase `ANON_KEY` is safe to expose — it's
unprivileged without an authenticated session.

---

## Cost Model (estimated)

All costs at personal/sporadic use levels.

| Item | Cost | Notes |
|---|---|---|
| Vercel (Hobby) | $0/mo | Free for personal projects |
| Supabase (Free) | $0/mo | Pauses after 1 week inactivity; upgrade to Pro ($25) if annoying |
| Cloudflare R2 | ~$0–2/mo | 10 GB free storage, $0 egress |
| Modal compute | ~$0.10–0.50/session | CPU seconds for transcription + ffmpeg; GPU optional |
| OpenAI Whisper | ~$0.006/min of audio | 10 min of video ≈ $0.06 |
| Anthropic Claude | ~$0.05–0.20/story gen | Depends on transcript length |
| **Total idle** | **~$0–5/mo** | |
| **Per session** | **~$0.20–1.00** | 10-min multi-clip project |

---

## Implementation Phases

### Phase 1 — Infrastructure + Manual Workflow (1–2 weeks)
- [ ] Scaffold Next.js app with Supabase auth (magic link, email allowlist)
- [ ] Upload UI: pick videos → presigned R2 PUT → progress bar
- [ ] Modal task: `transcribe_clip` + `merge_transcripts`
- [ ] Transcript viewer page (merged JSON rendered as readable text)
- [ ] Manual range picker (simple time inputs, no LLM yet)
- [ ] Modal task: `render_story` (calls existing `splice.py` logic)
- [ ] Download button

**Exit criteria:** Can do the full POC workflow from the phone, end to end.

### Phase 2 — LLM Story Generation (1 week)
- [ ] Modal task: `generate_stories` (Claude API, structured output)
- [ ] Story selection UI: 3 cards with title + excerpt + duration, tap to render
- [ ] Wire stories to existing render task

**Exit criteria:** Upload videos, app auto-generates and renders a story option.

### Phase 3 — Feedback Loop (1 week)
- [ ] Feedback text box beneath preview
- [ ] Re-generation prompt (previous story + feedback → revised ranges)
- [ ] Show history of iterations per project

**Exit criteria:** Can iterate on a cut via text feedback without leaving the phone.

### Phase 4 — Polish
- [ ] Resumable uploads (tus protocol) for large files / flaky mobile connections
- [ ] Inline transcript editor (adjust word timestamps before rendering)
- [ ] Multi-story compare view (render two options, swipe between)
- [ ] Render quality settings (resolution, bitrate)
- [ ] Project library (browse past projects)

---

## Open Questions to Resolve Before Starting

1. **Max input video length?** Affects Whisper chunk strategy and Modal
   timeout config. Suggest capping at 30 minutes total per project for now.

2. **Whisper API vs WhisperX on Modal GPU?** API is simpler; Modal GPU is
   faster and gives tighter timestamps without a second alignment pass.
   Start with API + `align.py` as a second pass (matches the POC), revisit
   if latency is annoying.

3. **How many story options?** 3 is a good default. More means longer LLM
   latency and more render time if you render all upfront. Consider
   generating 3 descriptions first, rendering only the one the user picks.

4. **Resumable uploads from day one?** iOS Safari can kill background
   uploads for large files. If your clips are typically under 500 MB and
   you keep the app in foreground during upload, regular multipart upload
   is fine. Add tus/resumable later if it becomes a problem.

5. **Supabase free tier pause:** if you use this app frequently, the pause
   won't be an issue. If it sits idle for weeks, consider a $25/mo Supabase
   Pro upgrade or a cron job that pings the DB to keep it alive.
