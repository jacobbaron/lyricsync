"""Modal app — lyricsync background compute tasks.

Deploy:
    modal deploy modal/app.py

Implements:
    P1-06  transcribe_clip          — Whisper word-level transcription
    P1-07  align_and_merge          — WhisperX alignment + global timeline merge
    P1-11  render_story             — ffmpeg multi-clip render
    VIS-01 analyze_visuals          — Gemini timestamped visual description (dev)

Secrets:
    Create a Modal secret named "lyricsync-secrets" containing:
        SUPABASE_URL
        SUPABASE_SERVICE_ROLE_KEY
        CLOUDFLARE_R2_ACCESS_KEY_ID
        CLOUDFLARE_R2_SECRET_ACCESS_KEY
        R2_BUCKET_NAME
        R2_ENDPOINT              # e.g. https://<account-id>.r2.cloudflarestorage.com
        OPENAI_API_KEY
        ANTHROPIC_API_KEY        # Claude story generation
        GEMINI_API_KEY           # Gemini visual analysis (VIS-01)
        MODAL_WEBHOOK_SECRET     # shared with Vercel

After deploying, note the web endpoint URLs printed by Modal and set them as
MODAL_TRANSCRIBE_URL, MODAL_ALIGN_URL, MODAL_RENDER_URL, MODAL_GENERATE_URL, and
MODAL_ANALYZE_URL in your Vercel environment variables.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path

import modal
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse

# Pure transcript formatting + quote-matching helpers (stdlib only, unit-tested).
# Modal automounts this sibling module when deploying app.py.
from transcript import (
    build_source_index,
    format_transcript,
    resolve_segments,
    stories_as_text,
)

# ---------------------------------------------------------------------------
# App + image
# ---------------------------------------------------------------------------

app = modal.App("lyricsync")

image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi[standard]>=0.115",
        "openai>=1.40",
        "boto3>=1.34",
        "supabase>=2.10",
    )
    # Modal 1.x no longer automounts sibling modules — add transcript.py explicitly.
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    # Title-card font for render-time text overlays (drawtext).
    .add_local_file(
        Path(__file__).parent / "assets" / "Montserrat.ttf",
        "/root/overlay_font.ttf",
    )
)

# Path to the bundled overlay font inside the container (see image above).
OVERLAY_FONT = "/root/overlay_font.ttf"

secrets = [modal.Secret.from_name("lyricsync-secrets")]

# Persistent cache of source clips for the render worker. Clips (often long —
# tens of minutes) are downloaded from R2 once and kept here, so re-rendering or
# iterating on the same footage skips the R2 download entirely.
render_cache = modal.Volume.from_name(
    "lyricsync-render-cache", create_if_missing=True
)
RENDER_CACHE_DIR = "/render_cache"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

AUDIO_BITRATE = "64k"
WHISPER_LIMIT_BYTES = 25 * 1_000_000  # 25 MB hard limit from OpenAI


def _r2():
    import boto3
    return boto3.client(
        "s3",
        endpoint_url=os.environ["R2_ENDPOINT"],
        aws_access_key_id=os.environ["CLOUDFLARE_R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["CLOUDFLARE_R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )


def _supabase():
    from supabase import create_client
    # Service role key bypasses RLS — only used server-side in Modal.
    return create_client(
        os.environ["SUPABASE_URL"],
        os.environ["SUPABASE_SERVICE_ROLE_KEY"],
    )


def _set_clip(sb, clip_id: str, **fields) -> None:
    sb.table("clips").update(fields).eq("id", clip_id).execute()


def _set_project(sb, project_id: str, **fields) -> None:
    sb.table("projects").update(fields).eq("id", project_id).execute()


# ---------------------------------------------------------------------------
# P1-06: Transcribe clip
# ---------------------------------------------------------------------------

@app.function(image=image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def transcribe_clip(request: Request) -> JSONResponse:
    """Vercel calls this endpoint; it authenticates, spawns the worker, and
    returns {"status": "accepted"} immediately so Vercel doesn't time out."""
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    clip_id = body.get("clip_id")
    if not clip_id:
        raise HTTPException(status_code=400, detail="clip_id required")

    _transcribe_worker.spawn(clip_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=image, secrets=secrets, timeout=600)
def _transcribe_worker(clip_id: str) -> None:
    """Downloads clip from R2, extracts audio, calls Whisper, writes result."""
    from openai import OpenAI

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    # Fetch clip row
    row = sb.table("clips").select(
        "id, r2_key, filename, project_id"
    ).eq("id", clip_id).maybe_single().execute()

    if not row.data:
        print(f"[transcribe] clip {clip_id} not found — skipping")
        return

    clip = row.data
    r2_key: str = clip["r2_key"]
    project_id: str = clip["project_id"]

    _set_clip(sb, clip_id, status="transcribing")
    # Also mark the project as transcribing if it's still at uploading.
    _set_project(sb, project_id, status="transcribing")

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 1. Download video
            ext = Path(r2_key).suffix or ".mp4"
            video_path = tmp / f"original{ext}"
            print(f"[transcribe] downloading {r2_key}")
            r2.download_file(bucket, r2_key, str(video_path))

            # 1b. Read recording timestamp from metadata for wall-clock display.
            recorded_at = _parse_creation_time(_get_creation_time(video_path))
            print(f"[transcribe] recorded_at={recorded_at!r}")

            # 2. Extract audio — 16 kHz mono MP3 (matches transcribe.py)
            audio_path = tmp / "audio.mp3"
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-i", str(video_path),
                    "-vn", "-ac", "1", "-ar", "16000",
                    "-b:a", AUDIO_BITRATE,
                    str(audio_path),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            # 3. Guard 25 MB Whisper limit
            audio_size = audio_path.stat().st_size
            if audio_size > WHISPER_LIMIT_BYTES:
                msg = (
                    f"Audio is {audio_size / 1e6:.1f} MB after extraction "
                    f"(Whisper limit 25 MB). Chunked transcription not yet "
                    f"supported — shorten the clip or split it manually."
                )
                _set_clip(sb, clip_id, status="error", error_message=msg)
                return

            # 4. Whisper transcription with word-level timestamps
            print(f"[transcribe] sending {audio_size / 1e6:.1f} MB to Whisper")
            client = OpenAI()
            with audio_path.open("rb") as f:
                resp = client.audio.transcriptions.create(
                    file=f,
                    model="whisper-1",
                    response_format="verbose_json",
                    timestamp_granularities=["word", "segment"],
                )
            transcript = resp.model_dump()

            # 5. Upload transcript.json to R2
            transcript_key = (
                f"projects/{project_id}/clips/{clip_id}/transcript.json"
            )
            r2.put_object(
                Bucket=bucket,
                Key=transcript_key,
                Body=json.dumps(transcript).encode(),
                ContentType="application/json",
            )
            print(f"[transcribe] wrote {transcript_key}")

            # 6. Update clip — transcribed_raw + store duration + transcript key
            duration = transcript.get("duration")
            update: dict = {
                "status": "transcribed_raw",
                "transcript_r2_key": transcript_key,
            }
            if duration is not None:
                update["duration_secs"] = float(duration)
            if recorded_at is not None:
                update["recorded_at"] = recorded_at
            _set_clip(sb, clip_id, **update)

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"[transcribe] error for clip {clip_id}: {msg}")
        _set_clip(sb, clip_id, status="error", error_message=msg[:500])


# ---------------------------------------------------------------------------
# P1-07: Align & Merge Task
# ---------------------------------------------------------------------------

# Separate image: CPU PyTorch + WhisperX wav2vec2 alignment model.
# Built separately from the transcription image to keep layer caching clean.
align_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .run_commands(
        # Install CPU-only PyTorch to avoid the multi-GB CUDA build.
        "pip install torch torchaudio --index-url https://download.pytorch.org/whl/cpu",
        # WhisperX pulls in faster-whisper, speechbrain, wav2vec2 alignment.
        "pip install whisperx",
    )
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
    )
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
)


def _get_creation_time(video_path: Path) -> str | None:
    """Read container creation timestamp via ffprobe (mirrors shorten/sync.py).

    Prefers com.apple.quicktime.creationdate (carries timezone offset, set at
    recording time) over the generic creation_time tag (often the export time).
    Returns None if ffprobe fails or no timestamp tag is present.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-print_format", "json",
            "-show_format", "-show_streams",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError:
        return None
    tags = (data.get("format", {}) or {}).get("tags", {}) or {}
    return (
        tags.get("com.apple.quicktime.creationdate")
        or tags.get("creation_time")
    )


def _parse_creation_time(raw: str | None) -> str | None:
    """Normalize a metadata creation timestamp to an ISO-8601 instant string
    Postgres timestamptz accepts, or None if it can't be parsed.

    Apple's creationdate carries a numeric offset like '-0700'; the generic
    creation_time tag is usually UTC ('...Z'). datetime.fromisoformat in 3.11
    handles both once 'Z' is rewritten to '+00:00'.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        return None


def _words_from(data: dict) -> list[dict]:
    """Normalize transcript dict to a flat list of {word, start, end}.

    Handles both Whisper API output (top-level 'words') and WhisperX output
    ('word_segments' or per-segment 'words').  Mirrors shorten/merge.py.
    """
    words = data.get("words") or data.get("word_segments")
    if not words:
        words = []
        for seg in data.get("segments", []):
            for w in seg.get("words", []) or []:
                words.append(w)
    out = []
    for w in words:
        if "start" not in w or "end" not in w:
            continue
        out.append({
            "word": (w.get("word") or w.get("text") or "").strip(),
            "start": float(w["start"]),
            "end": float(w["end"]),
        })
    return out


@app.function(image=align_image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def align_and_merge(request: Request) -> JSONResponse:
    """Vercel calls this endpoint; it authenticates, spawns the worker, and
    returns {"status": "accepted"} immediately so Vercel doesn't time out."""
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    project_id = body.get("project_id")
    if not project_id:
        raise HTTPException(status_code=400, detail="project_id required")

    _align_worker.spawn(project_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=align_image, secrets=secrets, timeout=1800)
def _align_worker(project_id: str) -> None:
    """Alignment + merge worker (P1-07).

    For every clip in the project:
      1. Download original video + raw Whisper transcript from R2.
      2. Extract audio, run WhisperX wav2vec2 alignment (phoneme-tight word
         timestamps).
      3. Upload transcript_aligned.json and update clip.transcript_r2_key.
      4. Compute global_start offsets from ffprobe creation timestamps
         (mirrors shorten/sync.py); update clip.global_start in DB.

    Then:
      5. Merge all aligned word lists onto a shared global timeline (mirrors
         shorten/merge.py); upload projects/<pid>/merged.json.
      6. Set all clip statuses to 'aligned' and project status to 'transcribed'.
    """
    from datetime import datetime

    import whisperx

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    # 1. Fetch all clips for this project
    result = sb.table("clips").select(
        "id, r2_key, filename, transcript_r2_key"
    ).eq("project_id", project_id).execute()
    clips = result.data or []

    if not clips:
        print(f"[align] no clips for project {project_id} — skipping")
        return

    print(f"[align] {len(clips)} clip(s) for project {project_id}")

    # Load alignment model once — avoids re-downloading the ~360 MB wav2vec2
    # weights for every clip.  English model; extend for multi-language later.
    print("[align] loading WhisperX wav2vec2 alignment model…")
    model_a, metadata = whisperx.load_align_model(
        language_code="en", device="cpu"
    )

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            clip_results: list[dict] = []

            for clip in clips:
                clip_id: str = clip["id"]
                r2_key: str = clip["r2_key"]
                filename: str = clip["filename"] or "clip.mp4"
                transcript_key: str = clip["transcript_r2_key"]

                print(f"[align] ── clip {clip_id} ({filename})")

                # a. Download original video
                ext = Path(r2_key).suffix or ".mp4"
                video_path = tmp / f"{clip_id}{ext}"
                r2.download_file(bucket, r2_key, str(video_path))

                # b. Read creation timestamp for global sync
                creation_time = _get_creation_time(video_path)
                print(f"[align]    creation_time={creation_time!r}")

                # c. Download raw Whisper transcript
                transcript_path = tmp / f"{clip_id}_transcript.json"
                r2.download_file(bucket, transcript_key, str(transcript_path))
                transcript = json.loads(transcript_path.read_text())

                # d. Re-extract audio (16 kHz mono) for WhisperX
                audio_path = tmp / f"{clip_id}_audio.mp3"
                subprocess.run(
                    [
                        "ffmpeg", "-y", "-i", str(video_path),
                        "-vn", "-ac", "1", "-ar", "16000",
                        "-b:a", AUDIO_BITRATE, str(audio_path),
                    ],
                    check=True,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )

                # e. Run WhisperX alignment
                segments = [
                    {
                        "text": s["text"],
                        "start": float(s["start"]),
                        "end": float(s["end"]),
                    }
                    for s in transcript.get("segments", [])
                    if "start" in s and "end" in s
                ]
                wav = whisperx.load_audio(str(audio_path))
                aligned = whisperx.align(
                    segments, model_a, metadata, wav, "cpu",
                    return_char_alignments=False,
                )
                aligned_data: dict = {
                    "language": transcript.get("language", "en"),
                    "duration": transcript.get("duration"),
                    "segments": aligned.get("segments", []),
                    "words": aligned.get("word_segments", []),
                }

                # f. Upload transcript_aligned.json
                aligned_key = (
                    f"projects/{project_id}/clips/{clip_id}"
                    f"/transcript_aligned.json"
                )
                r2.put_object(
                    Bucket=bucket,
                    Key=aligned_key,
                    Body=json.dumps(aligned_data).encode(),
                    ContentType="application/json",
                )
                print(f"[align]    wrote {aligned_key}")

                clip_results.append({
                    "clip_id": clip_id,
                    "filename": filename,
                    "r2_key": r2_key,
                    "aligned_key": aligned_key,
                    "aligned_data": aligned_data,
                    "creation_time": creation_time,
                })

            # 2. Compute global_start from ffprobe creation timestamps
            for cr in clip_results:
                ct = cr["creation_time"]
                cr["_dt"] = None
                if ct:
                    try:
                        cr["_dt"] = datetime.fromisoformat(
                            ct.replace("Z", "+00:00")
                        )
                    except ValueError:
                        pass

            dts = [cr["_dt"] for cr in clip_results if cr["_dt"] is not None]
            if dts:
                anchor = min(dts)
                for cr in clip_results:
                    if cr["_dt"] is not None:
                        cr["global_start"] = (cr["_dt"] - anchor).total_seconds()
                    else:
                        cr["global_start"] = 0.0
            else:
                # No usable creation timestamps (e.g. compressed/re-encoded
                # before upload).  Fall back to 0 for all clips — acceptable
                # for single-camera recordings or pre-synced footage.
                print("[align] no creation timestamps found — global_start=0 for all clips")
                for cr in clip_results:
                    cr["global_start"] = 0.0

            # 3. Build merged.json (shape required by P1-09 and P1-11)
            all_words: list[dict] = []
            for cr in clip_results:
                offset = cr["global_start"]
                for w in _words_from(cr["aligned_data"]):
                    all_words.append({
                        "text": w["word"],
                        "global_start": w["start"] + offset,
                        "global_end": w["end"] + offset,
                        "local_start": w["start"],
                        "local_end": w["end"],
                        "source": cr["filename"],
                        "source_path": cr["r2_key"],
                    })
            all_words.sort(key=lambda w: w["global_start"])

            merged_key = f"projects/{project_id}/merged.json"
            r2.put_object(
                Bucket=bucket,
                Key=merged_key,
                Body=json.dumps({"words": all_words}).encode(),
                ContentType="application/json",
            )
            print(f"[align] wrote {merged_key} with {len(all_words)} words")

            # 4. Update DB
            for cr in clip_results:
                _set_clip(
                    sb, cr["clip_id"],
                    status="aligned",
                    global_start=cr["global_start"],
                    # Point transcript_r2_key at the better aligned transcript.
                    transcript_r2_key=cr["aligned_key"],
                )
            _set_project(sb, project_id, status="transcribed")
            print(f"[align] project {project_id} → transcribed ✓")

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"[align] error for project {project_id}: {msg}")
        _set_project(sb, project_id, status="error", error_message=msg[:500])


# ---------------------------------------------------------------------------
# P2-02: Generate Stories Task
# ---------------------------------------------------------------------------

# Separate image: adds the Anthropic SDK.  No heavy ML deps needed here.
gen_image = (
    modal.Image.debian_slim(python_version="3.11")
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
        "anthropic>=0.34",
    )
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
)

# Tool definition passed to Claude — forces structured JSON output.
# Claude returns verbatim quotes; the code resolves them to timestamps.
_PROPOSE_STORIES_TOOL = {
    "name": "propose_stories",
    "description": (
        "Propose exactly 3 story cuts from the transcript. Each story is an "
        "ordered list of verbatim quotes drawn from the source clips."
    ),
    "input_schema": {
        "type": "object",
        "required": ["stories"],
        "properties": {
            "stories": {
                "type": "array",
                "minItems": 3,
                "maxItems": 3,
                "items": {
                    "type": "object",
                    "required": ["title", "description", "segments"],
                    "properties": {
                        "title": {
                            "type": "string",
                            "description": "Short, evocative title for this story cut",
                        },
                        "description": {
                            "type": "string",
                            "description": (
                                "2-3 sentences on what makes this story "
                                "work editorially"
                            ),
                        },
                        "segments": {
                            "type": "array",
                            "minItems": 1,
                            "items": {
                                "type": "object",
                                "required": ["source", "quote"],
                                "properties": {
                                    "source": {
                                        "type": "string",
                                        "description": (
                                            "Exact filename from the transcript "
                                            "section header (e.g. IMG_2415.mov)"
                                        ),
                                    },
                                    "quote": {
                                        "type": "string",
                                        "description": (
                                            "A verbatim, contiguous excerpt copied "
                                            "from that source's transcript. Becomes "
                                            "one continuous cut in the video."
                                        ),
                                    },
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}

_SYSTEM_PROMPT = """\
You are a creative video editor. You have been given a merged transcript from \
a multi-camera live recording session.

The transcript is grouped by source clip under headers like \
"=== IMG_2415.mov ===". Each paragraph is a continuous run of speech.

Your job is to propose compelling short-form story cuts from this footage. \
Each cut is an ordered list of segments drawn from any clip in any order.

To specify a segment, give:
- source: the exact filename from the section header
- quote: a verbatim, contiguous excerpt copied from that source's transcript

Each quote becomes one continuous cut. The system maps your quotes back to \
precise video timestamps automatically — you never deal with timestamps. Copy \
quotes exactly as they appear so they can be matched.

Guidelines:
- Each story should total roughly 30–180 seconds of speech
- Quote complete thoughts or sentences — never isolated words
- To skip a boring middle, use two separate segments instead of one long quote
- Make the 3 options meaningfully distinct: different angles, moments, or arcs
- You may interleave clips (e.g. cut between cameras mid-conversation)
- The source must exactly match a filename from the transcript headers\
"""


def _build_messages(
    transcript_text: str,
    current_round: dict,
    prev_rounds: list[dict],
    sb,
) -> list[dict]:
    """Build the Claude messages array from round history."""
    messages: list[dict] = []

    # Round 1 user message always includes the full transcript
    round1_prompt = prev_rounds[0]["prompt"] if prev_rounds else current_round["prompt"]
    first_content = f"Transcript:\n\n{transcript_text}"
    if round1_prompt:
        first_content += f"\n\nUser's request: {round1_prompt}"
    first_content += (
        "\n\nPropose 3 story options using the propose_stories tool, "
        "quoting verbatim transcript text for each segment."
    )
    messages.append({"role": "user", "content": first_content})

    # Alternate assistant/user turns for each previous round.
    # Skip rounds with no completed stories (aborted/failed rounds) to avoid
    # consecutive user messages, which the Claude API rejects.
    completed_rounds = []
    for rnd in prev_rounds:
        result = sb.table("stories").select(
            "title, description, estimated_duration_secs, ranges_json"
        ).eq("generation_round_id", rnd["id"]).order("created_at").execute()
        stories = [
            {
                "title": s["title"],
                "description": s["description"],
                "estimated_duration_secs": s["estimated_duration_secs"],
                "ranges": s["ranges_json"],
            }
            for s in (result.data or [])
            if s["title"]
        ]
        if stories:
            completed_rounds.append((rnd, stories))

    for i, (rnd, stories) in enumerate(completed_rounds):
        messages.append({
            "role": "assistant",
            "content": "Here are my suggestions:\n\n" + stories_as_text(stories),
        })
        is_last = i == len(completed_rounds) - 1
        if is_last:
            followup = current_round["prompt"] or "Generate 3 new story options."
        else:
            next_rnd = completed_rounds[i + 1][0]
            followup = next_rnd["prompt"] or "Generate 3 new story options."
        messages.append({
            "role": "user",
            "content": f"{followup}\n\nPropose 3 new story options.",
        })

    return messages


@app.function(image=gen_image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def generate_stories(request: Request) -> JSONResponse:
    """Vercel calls this endpoint; it authenticates, spawns the generation
    worker, and returns {"status": "accepted"} immediately."""
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    project_id = body.get("project_id")
    round_id = body.get("round_id")
    if not project_id or not round_id:
        raise HTTPException(status_code=400, detail="project_id and round_id required")

    _generate_worker.spawn(project_id, round_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=gen_image, secrets=secrets, timeout=300)
def _generate_worker(project_id: str, round_id: str) -> None:
    """Story generation worker (P2-02).

    1. Load merged.json transcript from R2.
    2. Load the current generation round + all previous rounds from DB.
    3. Build a multi-turn conversation history so Claude has full context.
    4. Call Claude with extended thinking + tool use to get 3 stories of quotes.
    5. Resolve each verbatim quote to a precise timestamp range (fail loud).
    6. Fill in the 3 placeholder story rows and advance status to 'stories_ready'.
    """
    import anthropic

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    try:
        # 1. Load merged.json
        merged_key = f"projects/{project_id}/merged.json"
        obj = r2.get_object(Bucket=bucket, Key=merged_key)
        merged = json.loads(obj["Body"].read())
        words = merged.get("words", [])
        if not words:
            raise ValueError("merged.json is empty — run alignment first")

        transcript_text = format_transcript(words)

        # 2. Load current round
        round_result = sb.table("generation_rounds").select(
            "id, round, prompt"
        ).eq("id", round_id).limit(1).execute()
        rows = round_result.data or []
        if not rows:
            raise ValueError(f"generation round {round_id} not found")
        current_round = rows[0]
        current_round_num: int = current_round["round"]

        # 3. Load all *previous* rounds in order (rounds before current)
        prev = sb.table("generation_rounds").select(
            "id, round, prompt"
        ).eq("project_id", project_id).lt(
            "round", current_round_num
        ).order("round").execute()
        prev_rounds: list[dict] = prev.data or []

        print(
            f"[generate] project {project_id} round {current_round_num} "
            f"(history: {len(prev_rounds)} prior round(s), "
            f"{len(words)} words in transcript)"
        )

        # 4. Build messages and call Claude
        messages = _build_messages(
            transcript_text=transcript_text,
            current_round=current_round,
            prev_rounds=prev_rounds,
            sb=sb,
        )

        client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        response = client.messages.create(
            model="claude-opus-4-5",
            max_tokens=16000,
            thinking={"type": "enabled", "budget_tokens": 10000},
            system=_SYSTEM_PROMPT,
            tools=[_PROPOSE_STORIES_TOOL],
            tool_choice={"type": "auto"},
            messages=messages,
        )

        tool_use = next(
            (b for b in response.content if b.type == "tool_use"), None
        )
        if not tool_use:
            raise ValueError("Claude did not call the propose_stories tool")

        stories: list[dict] = tool_use.input["stories"]
        print(f"[generate] Claude returned {len(stories)} stories")

        # 5. Resolve every quote to a timestamp range BEFORE touching the DB,
        #    so a failed match leaves no partially-written round behind.
        index_by_source = build_source_index(words)

        resolved: list[dict] = []
        for story_data in stories:
            print(f"[generate] story '{story_data['title']}' — segments Claude chose:")
            for seg in story_data.get("segments", []):
                print(f"  [{seg.get('source')}] quote: {seg.get('quote', '')[:120]!r}")
            ranges = resolve_segments(story_data["segments"], index_by_source)
            print(f"[generate]   resolved ranges:")
            for r in ranges:
                print(f"    [{r['source']}] {r['start']:.2f}s–{r['end']:.2f}s  text: {r['text'][:120]!r}")
            duration = sum(r["end"] - r["start"] for r in ranges)
            resolved.append({
                "title": story_data["title"],
                "description": story_data["description"],
                "estimated_duration_secs": round(duration, 1),
                "ranges_json": ranges,
                "status": "ready",
            })

        # 6. Fill in the placeholder story rows (created by the Vercel route)
        existing = sb.table("stories").select("id").eq(
            "generation_round_id", round_id
        ).order("created_at").execute()
        story_ids = [s["id"] for s in (existing.data or [])]

        for payload, story_id in zip(resolved, story_ids):
            sb.table("stories").update(payload).eq("id", story_id).execute()

        # 6. Advance project
        sb.table("projects").update({
            "status": "stories_ready"
        }).eq("id", project_id).execute()

        print(f"[generate] project {project_id} → stories_ready ✓")

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"[generate] error for project {project_id}: {msg}")
        try:
            sb.table("projects").update({
                "status": "error",
                "error_message": msg[:500],
            }).eq("id", project_id).execute()
        except Exception as db_exc:
            print(f"[generate] ALSO failed to write error to DB: {db_exc}")


# ---------------------------------------------------------------------------
# VIS-01: Analyze Visuals Task (development / experimentation harness)
# ---------------------------------------------------------------------------
#
# Sends a clip to Gemini and asks for a timestamped description of what's on
# screen (segments + highlight beats), so we can evaluate whether vision adds
# useful editorial signal before wiring it into story generation.
#
# Built as an A/B harness: the request picks a `variant` (model + prompt
# strategy + media resolution). Each run is stored as its own visual_analyses
# row so variants can be compared side by side, and the full Gemini round-trip
# (prompt, raw response, token usage, timings, file state, traceback) is kept in
# the row's `debug` column for diagnosis via the API.

analyze_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
        "google-genai>=0.3",
    )
    # transcript.py must be present because app.py imports it at the module level;
    # Modal's cloudpickle serialization captures those globals and the container
    # crashes on startup if the module can't be imported.
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    .add_local_file(Path(__file__).parent / "visual.py", "/root/visual.py")
)

# Variant registry — each is one "approach" to try. `media_resolution` is
# applied best-effort (older SDKs ignore it). Add rows here to trial more.
# Each variant keys:
#   model            — Gemini model id (gemini-3.5-flash is the current flash tier).
#   strategy         — prompt builder strategy (see visual.build_prompt).
#   media_resolution — None (default) | MEDIA_RESOLUTION_LOW|MEDIUM|HIGH; HIGH
#                      gives more tokens/frame → finer facial-expression detail.
#   fps              — frame sampling rate (Gemini defaults to 1 fps); raise to
#                      catch fleeting expressions. None leaves the default.
#   needs_transcript — download the clip's aligned transcript and feed it to the
#                      prompt as ground-truth speech.
VISUAL_VARIANTS: dict[str, dict] = {
    # Cheapest pass: a coarse 1-3 sentence visual note (summary only, low media
    # resolution) to store on the clip as transcript-side context. store_summary
    # writes result.summary into clips.visual_description.
    "context": {
        "model": "gemini-3.5-flash",
        "strategy": "context",
        "media_resolution": "MEDIA_RESOLUTION_LOW",
        "fps": None,
        "needs_transcript": False,
        "store_summary": True,
    },
    # The chosen default: cheap, fast, descriptions + highlight beats.
    "flash": {
        "model": "gemini-3.5-flash",
        "strategy": "default",
        "media_resolution": None,
        "fps": None,
        "needs_transcript": False,
    },
    # Same model/prompt at low media resolution — cheaper in tokens; use to
    # judge how much visual detail we actually lose.
    "flash_lowres": {
        "model": "gemini-3.5-flash",
        "strategy": "default",
        "media_resolution": "MEDIA_RESOLUTION_LOW",
        "fps": None,
        "needs_transcript": False,
    },
    # Editorial prompt — also returns ready-to-render suggested_clips.
    "editorial": {
        "model": "gemini-3.5-flash",
        "strategy": "editorial",
        "media_resolution": None,
        "fps": None,
        "needs_transcript": False,
    },
    # Audio + visual: lean into Gemini hearing the audio. High media resolution
    # and denser frame sampling for detailed facial-expression + vocal-tone reads.
    "audio_aware": {
        "model": "gemini-3.5-flash",
        "strategy": "audio_aware",
        "media_resolution": "MEDIA_RESOLUTION_HIGH",
        "fps": 3,
        "needs_transcript": False,
    },
    # Like audio_aware, but the aligned transcript is supplied as ground-truth
    # text so the model relates visuals to speech without mis-hearing words.
    "with_transcript": {
        "model": "gemini-3.5-flash",
        "strategy": "transcript",
        "media_resolution": "MEDIA_RESOLUTION_HIGH",
        "fps": 3,
        "needs_transcript": True,
    },
    # Disabled — gemini-2.5-pro / 3.x pro tiers are ~10x flash and gave only
    # marginally better selection in testing. Re-enable here if you want the
    # quality ceiling back.
    # "pro": {
    #     "model": "gemini-3.1-pro-preview",
    #     "strategy": "default",
    #     "media_resolution": None,
    #     "fps": None,
    #     "needs_transcript": False,
    # },
}
DEFAULT_VISUAL_VARIANT = "flash"


def _set_analysis(sb, analysis_id: str, **fields) -> None:
    sb.table("visual_analyses").update(fields).eq("id", analysis_id).execute()


def _format_clip_transcript(tdata: dict) -> str:
    """Render transcript_aligned.json as compact timestamped lines for a prompt.

    The aligned file is {language, duration, segments:[{text,start,end}], words}.
    Prefer per-segment lines (clip-local seconds); fall back to per-word.
    """
    lines: list[str] = []
    for seg in tdata.get("segments", []) or []:
        text = str(seg.get("text") or "").strip()
        if not text:
            continue
        start = seg.get("start")
        end = seg.get("end")
        if isinstance(start, (int, float)) and isinstance(end, (int, float)):
            lines.append(f"[{float(start):.1f}-{float(end):.1f}s] {text}")
        else:
            lines.append(text)
    if not lines:  # no usable segments — stitch the word stream instead
        words = [
            str(w.get("word") or "").strip()
            for w in (tdata.get("words") or [])
            if w.get("word")
        ]
        if words:
            lines.append(" ".join(words))
    return "\n".join(lines)


def _probe_duration(video_path: Path) -> float | None:
    """Return clip duration in seconds via ffprobe, or None if it can't be read."""
    result = subprocess.run(
        [
            "ffprobe", "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        return float(result.stdout.strip())
    except (ValueError, AttributeError):
        return None


@app.function(image=analyze_image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def analyze_visuals(request: Request) -> JSONResponse:
    """Vercel calls this endpoint; it authenticates, spawns the analysis worker,
    and returns {"status": "accepted"} immediately.

    Body: {"analysis_id": "<uuid>"} — the worker reads the clip + variant off
    the pre-created visual_analyses row.
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    analysis_id = body.get("analysis_id")
    if not analysis_id:
        raise HTTPException(status_code=400, detail="analysis_id required")

    _analyze_worker.spawn(analysis_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=analyze_image, secrets=secrets, timeout=600)
def _analyze_worker(analysis_id: str) -> None:
    """Visual analysis worker (VIS-01).

    1. Load the visual_analyses row (variant) + its clip (r2_key, duration).
    2. Download the clip from R2.
    3. Upload it to the Gemini File API, poll until ACTIVE.
    4. Ask the variant's model for timestamped visual JSON.
    5. Parse + store result; persist the full round-trip in `debug`.
    """
    import time
    import traceback

    from visual import build_prompt, format_visual_track, parse_visual_response

    # sb is initialized before the try block so the except can write errors back.
    # Use limit(1) instead of maybe_single() — maybe_single() throws an
    # AttributeError in supabase-py 2.10+ (same bug fixed in the generate worker).
    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    # Diagnostic record — written back even on failure for API-side debugging.
    debug: dict = {"steps": []}

    def step(msg: str) -> None:
        print(f"[analyze:{analysis_id}] {msg}")
        debug["steps"].append(msg)

    # 1. Load analysis row (limit(1) avoids the maybe_single() crash in supabase-py 2.10+)
    row = sb.table("visual_analyses").select(
        "id, clip_id, variant"
    ).eq("id", analysis_id).limit(1).execute()
    if not row.data:
        print(f"[analyze] analysis {analysis_id} not found — skipping")
        return
    analysis = row.data[0]
    clip_id = analysis["clip_id"]
    variant_name = analysis["variant"] or DEFAULT_VISUAL_VARIANT
    variant = VISUAL_VARIANTS.get(variant_name, VISUAL_VARIANTS[DEFAULT_VISUAL_VARIANT])
    debug["variant"] = variant_name
    debug["variant_config"] = variant

    _set_analysis(sb, analysis_id, status="analyzing")

    clip_row = sb.table("clips").select(
        "id, r2_key, filename, project_id, duration_secs, transcript_r2_key"
    ).eq("id", clip_id).limit(1).execute()
    if not clip_row.data:
        _set_analysis(
            sb, analysis_id, status="error",
            error="clip not found", debug=debug,
        )
        return
    clip = clip_row.data[0]
    r2_key = clip["r2_key"]
    project_id = clip["project_id"]

    try:
        from google import genai
        from google.genai import types

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 2. Download the clip
            ext = Path(r2_key).suffix or ".mp4"
            video_path = tmp / f"clip{ext}"
            step(f"downloading {r2_key}")
            r2.download_file(bucket, r2_key, str(video_path))
            debug["file_size_bytes"] = video_path.stat().st_size

            duration = clip.get("duration_secs") or _probe_duration(video_path)
            debug["duration_secs"] = duration

            # Fetch the aligned transcript as ground-truth speech for the
            # transcript strategy. Best-effort: if it's missing we still run,
            # just without the transcript block.
            transcript_text = None
            if variant.get("needs_transcript"):
                tkey = clip.get("transcript_r2_key")
                if tkey:
                    try:
                        tpath = tmp / "transcript.json"
                        r2.download_file(bucket, tkey, str(tpath))
                        tdata = json.loads(tpath.read_text())
                        transcript_text = _format_clip_transcript(tdata)
                        step(f"loaded transcript ({len(transcript_text)} chars)")
                    except Exception as t_exc:  # noqa: BLE001
                        step(f"transcript fetch failed (non-fatal): {t_exc}")
                else:
                    step("no transcript_r2_key on clip — running without it")
                debug["transcript_text"] = transcript_text

            prompt = build_prompt(
                duration,
                strategy=variant["strategy"],
                transcript_text=transcript_text,
            )
            debug["model"] = variant["model"]
            debug["prompt"] = prompt

            client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])

            # 3. Upload to the Gemini File API and wait until processed.
            t0 = time.time()
            step("uploading to Gemini File API")
            gfile = client.files.upload(file=str(video_path))
            while getattr(gfile.state, "name", str(gfile.state)) == "PROCESSING":
                time.sleep(2)
                gfile = client.files.get(name=gfile.name)
            state = getattr(gfile.state, "name", str(gfile.state))
            debug["gemini_file_name"] = gfile.name
            debug["gemini_file_state"] = state
            debug["upload_secs"] = round(time.time() - t0, 2)
            if state != "ACTIVE":
                raise RuntimeError(f"Gemini file processing ended in state {state}")

            # 4. Generate. media_resolution is best-effort across SDK versions.
            cfg_kwargs: dict = {"response_mime_type": "application/json", "temperature": 0.4}
            mr = variant.get("media_resolution")
            if mr:
                try:
                    cfg_kwargs["media_resolution"] = getattr(types.MediaResolution, mr)
                except Exception as mr_exc:  # noqa: BLE001
                    step(f"media_resolution {mr} unsupported by SDK: {mr_exc}")

            # Custom frame sampling (fps) rides on the video Part's
            # video_metadata; default is 1 fps which is too coarse for fleeting
            # expressions. Build an explicit Part referencing the uploaded file
            # so we can attach it; fall back to the plain form if unsupported.
            fps = variant.get("fps")
            video_part: object = gfile
            if fps:
                try:
                    video_part = types.Part(
                        file_data=types.FileData(
                            file_uri=gfile.uri, mime_type=gfile.mime_type
                        ),
                        video_metadata=types.VideoMetadata(fps=fps),
                    )
                    step(f"sampling at fps={fps}")
                except Exception as fps_exc:  # noqa: BLE001
                    step(f"fps={fps} unsupported by SDK: {fps_exc}")
                    video_part = gfile

            t1 = time.time()
            step(f"generate_content model={variant['model']}")
            resp = client.models.generate_content(
                model=variant["model"],
                contents=[video_part, prompt],
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            debug["generate_secs"] = round(time.time() - t1, 2)

            raw_text = resp.text or ""
            debug["raw_response"] = raw_text

            usage = getattr(resp, "usage_metadata", None)
            if usage is not None:
                debug["usage"] = {
                    "prompt_tokens": getattr(usage, "prompt_token_count", None),
                    "candidates_tokens": getattr(usage, "candidates_token_count", None),
                    "total_tokens": getattr(usage, "total_token_count", None),
                }

            # 5. Parse + persist
            visual = parse_visual_response(raw_text, duration)
            debug["counts"] = {
                "segments": len(visual["segments"]),
                "highlights": len(visual["highlights"]),
                "suggested_clips": len(visual.get("suggested_clips", [])),
            }
            step(
                f"parsed {debug['counts']['segments']} segments, "
                f"{debug['counts']['highlights']} highlights"
            )
            print(format_visual_track(visual))

            # Best-effort cleanup of the uploaded Gemini file.
            try:
                client.files.delete(name=gfile.name)
            except Exception as del_exc:  # noqa: BLE001
                step(f"file cleanup failed (non-fatal): {del_exc}")

            # Persist the parsed track to R2 too, for parity with transcripts.
            result_key = (
                f"projects/{project_id}/clips/{clip_id}"
                f"/visual.{variant_name}.json"
            )
            r2.put_object(
                Bucket=bucket,
                Key=result_key,
                Body=json.dumps(visual).encode(),
                ContentType="application/json",
            )

            _set_analysis(
                sb, analysis_id,
                status="done",
                result=visual,
                result_r2_key=result_key,
                debug=debug,
                error=None,
            )

            # For the coarse "context" pass, store the summary on the clip so
            # downstream story generation can reference it next to the transcript.
            if variant.get("store_summary") and visual.get("summary"):
                sb.table("clips").update(
                    {"visual_description": visual["summary"]}
                ).eq("id", clip_id).execute()
                step("stored clip visual_description")

            step("done ✓")

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        debug["traceback"] = traceback.format_exc()
        print(f"[analyze] error for analysis {analysis_id}: {msg}")
        _set_analysis(
            sb, analysis_id, status="error", error=msg[:1000], debug=debug,
        )


# ---------------------------------------------------------------------------
# P1-11: Render Story Task
# ---------------------------------------------------------------------------

# Reuse the base image — ffmpeg, boto3 and supabase are already present.
# No PyTorch or WhisperX needed for rendering.

# ffmpeg output params — mirrors shorten/splice.py
_RENDER_PAD_S = 0.08        # seconds of padding around each trim point
_RENDER_W = 1080
_RENDER_H = 1920
_RENDER_FPS = 30
_RENDER_SR = 48000          # audio sample rate


def _build_drawtext(
    overlay: dict, text_path: Path, seg_len: float
) -> str:
    """Build a drawtext filter for a per-segment text overlay (title card).

    overlay keys (all but `text` optional):
      text     — the overlay copy (wrapped automatically).
      in       — seconds into the segment to fade in (default 0).
      out      — seconds into the segment to remove (default = segment length).
      size     — font size in px (default 64).
      position — "center" | "upper" | "lower" (default "center").
      wrap     — max characters per line before wrapping (default 22).

    The text is written to `text_path` and referenced via textfile= so we never
    have to escape colons/quotes/commas in the copy itself. Times are
    segment-local (each segment's PTS is reset to 0 before this runs).
    """
    import textwrap

    raw = str(overlay.get("text") or "").strip()
    wrap = int(overlay.get("wrap") or 22)
    wrapped = "\n".join(textwrap.wrap(raw, width=wrap)) or raw
    text_path.write_text(wrapped)

    size = int(overlay.get("size") or 64)
    pos = str(overlay.get("position") or "center").lower()
    yexpr = {
        "upper": "h*0.10",
        "lower": "h*0.72",
    }.get(pos, "(h-text_h)/2")

    t_in = float(overlay.get("in") or 0.0)
    t_out = float(overlay["out"]) if overlay.get("out") is not None else seg_len
    # Commas inside the enable expression must be escaped within filter_complex.
    enable = f"between(t\\,{t_in:.3f}\\,{t_out:.3f})"

    return (
        f"drawtext=fontfile={OVERLAY_FONT}:textfile={text_path}:"
        f"fontcolor=white:fontsize={size}:line_spacing=14:"
        f"box=1:boxcolor=black@0.5:boxborderw=30:"
        f"x=(w-text_w)/2:y={yexpr}:enable='{enable}'"
    )


@app.function(image=image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def render_story(request: Request) -> JSONResponse:
    """Vercel calls this endpoint; it authenticates, spawns the render worker,
    and returns {"status": "accepted"} immediately so Vercel doesn't time out."""
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    story_id = body.get("story_id")
    if not story_id:
        raise HTTPException(status_code=400, detail="story_id required")

    _render_worker.spawn(story_id)
    return JSONResponse({"status": "accepted"})


@app.function(
    image=image,
    secrets=secrets,
    timeout=1800,
    volumes={RENDER_CACHE_DIR: render_cache},
)
def _render_worker(story_id: str) -> None:
    """Render worker (P1-11).

    1. Load story from DB (ranges_json, project_id).
    2. Resolve each range's source filename to a clip r2_key.
    3. Reuse cached source clips from the render-cache volume; download any
       missing ones from R2 once and keep them for future renders.
    4. Run ffmpeg filter_complex splice (mirrors shorten/splice.py):
         - trim + normalize resolution/fps per segment
         - concat all segments
         - BT.709 8-bit yuv420p output for iPhone HDR compatibility
    5. Upload output.mp4 to R2 at projects/<pid>/stories/<sid>/output.mp4.
    6. Update story: status='done', render_r2_key set.
       Project status is intentionally left unchanged ('transcribed').
    """
    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    # 1. Fetch story
    row = sb.table("stories").select(
        "id, project_id, ranges_json, status"
    ).eq("id", story_id).limit(1).execute()
    rows = row.data or []

    if not rows:
        print(f"[render] story {story_id} not found — skipping")
        return

    story = rows[0]
    project_id: str = story["project_id"]
    ranges: list[dict] = story["ranges_json"] or []

    if not ranges:
        msg = "story has no ranges"
        sb.table("stories").update({
            "status": "error", "error_message": msg
        }).eq("id", story_id).execute()
        return

    # 2. Fetch all clips for this project to resolve source → r2_key
    clips_row = sb.table("clips").select(
        "id, filename, r2_key"
    ).eq("project_id", project_id).execute()
    clip_map: dict[str, str] = {
        c["filename"]: c["r2_key"] for c in (clips_row.data or [])
    }

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 3. Resolve each unique source clip to a local file, reusing the
            # persistent render-cache volume. Only clips not already cached are
            # downloaded from R2; everything else is reused across renders.
            # inputs: r2_key → (local_path, ffmpeg_input_index)
            cache_dir = Path(RENDER_CACHE_DIR)
            cache_dir.mkdir(parents=True, exist_ok=True)
            render_cache.reload()  # see clips cached by earlier render runs

            inputs: dict[str, tuple[Path, int]] = {}
            downloaded_any = False
            for rng in ranges:
                source: str = rng["source"]
                if source == "blank":
                    continue
                r2_key = clip_map.get(source)
                if not r2_key:
                    raise ValueError(f"source clip not found in project: {source!r}")
                if r2_key not in inputs:
                    # Cache key mirrors the R2 key so it's unique per clip.
                    cached = cache_dir / r2_key.replace("/", "_")
                    if cached.exists() and cached.stat().st_size > 0:
                        print(f"[render] cache hit {r2_key}")
                    else:
                        print(f"[render] downloading {r2_key}")
                        # Stage the download inside the cache dir (same
                        # filesystem) so the atomic rename works — a temp dir on
                        # another device would raise EXDEV on replace().
                        staging = cached.with_name(cached.name + ".part")
                        r2.download_file(bucket, r2_key, str(staging))
                        staging.replace(cached)
                        downloaded_any = True
                    inputs[r2_key] = (cached, len(inputs))

            # Persist any newly downloaded clips so future renders reuse them.
            if downloaded_any:
                render_cache.commit()

            # 4. Build ffmpeg filter_complex (mirrors shorten/splice.py exactly).
            W, H = _RENDER_W, _RENDER_H
            FPS, SR = _RENDER_FPS, _RENDER_SR
            vnorm = (
                f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={FPS}"
            )

            # One seeked ffmpeg input PER range: `-ss start -t dur -i file`.
            # Input seeking (accurate_seek is on by default) is both
            # frame-accurate and fast — ffmpeg jumps to the start instead of
            # decoding from 0 — and giving each segment its own input avoids the
            # filter-graph trim/concat mis-timing that occurs when many trims
            # share one long input.
            # seek_inputs entries: (extra_cmd_args, Path|None) — Path is None for
            # synthetic "blank" segments, which are sourced via lavfi instead of
            # a seeked file input.
            seek_inputs: list[list[str]] = []
            parts = []
            in_idx = 0
            for i, rng in enumerate(ranges):
                source = rng["source"]
                if source == "blank":
                    dur = float(rng["end"]) - float(rng["start"])
                    seek_inputs.append([
                        "-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:d={dur:.3f}:r={FPS}",
                        "-f", "lavfi", "-i", f"anullsrc=channel_layout=stereo:sample_rate={SR}",
                    ])
                    vidx, aidx = in_idx, in_idx + 1
                    in_idx += 2
                    vchain = f"[{vidx}:v]setpts=PTS-STARTPTS,{vnorm}"
                    overlay = rng.get("overlay")
                    if overlay and overlay.get("text"):
                        draw = _build_drawtext(
                            overlay, tmp / f"overlay_{i}.txt", seg_len=dur
                        )
                        vchain += f",{draw}"
                    parts.append(
                        f"{vchain}[v{i}];"
                        f"[{aidx}:a]asetpts=PTS-STARTPTS,"
                        f"aresample={SR},aformat=channel_layouts=stereo,"
                        f"atrim=duration={dur:.3f}[a{i}]"
                    )
                    continue

                r2_key = clip_map[source]
                local_path, _ = inputs[r2_key]
                a = max(0.0, float(rng["start"]) - _RENDER_PAD_S)
                dur = (float(rng["end"]) + _RENDER_PAD_S) - a
                seek_inputs.append(["-ss", f"{a:.3f}", "-t", f"{dur:.3f}", "-i", str(local_path)])
                vidx = in_idx
                in_idx += 1

                # Each input is already trimmed to the window; just reset PTS,
                # normalise, and (optionally) draw the title-card overlay.
                vchain = f"[{vidx}:v]setpts=PTS-STARTPTS,{vnorm}"
                overlay = rng.get("overlay")
                if overlay and overlay.get("text"):
                    draw = _build_drawtext(
                        overlay, tmp / f"overlay_{i}.txt", seg_len=dur
                    )
                    vchain += f",{draw}"
                parts.append(
                    f"{vchain}[v{i}];"
                    f"[{vidx}:a]asetpts=PTS-STARTPTS,"
                    f"aresample={SR},aformat=channel_layouts=stereo[a{i}]"
                )
            concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(ranges)))
            parts.append(
                f"{concat_in}concat=n={len(ranges)}:v=1:a=1[vc][a];"
                "[vc]format=yuv420p[v]"
            )
            filter_complex = ";".join(parts)

            output_path = tmp / "output.mp4"

            # Build command: one seeked input per range (order = filter indices).
            cmd = ["ffmpeg", "-y"]
            for input_args in seek_inputs:
                cmd += input_args
            cmd += [
                "-filter_complex", filter_complex,
                "-map", "[v]", "-map", "[a]",
                # Video codec — iPhone-friendly BT.709 8-bit
                "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-profile:v", "high",
                "-colorspace", "bt709", "-color_primaries", "bt709",
                "-color_trc", "bt709", "-color_range", "tv",
                # Audio codec
                "-c:a", "aac", "-b:a", "160k",
                # Optimise for streaming / progressive download
                "-movflags", "+faststart",
                str(output_path),
            ]

            print(
                f"[render] ffmpeg: {len(ranges)} segment(s) from "
                f"{len(inputs)} source(s)"
            )
            proc = subprocess.run(cmd, capture_output=True, text=True)
            if proc.returncode != 0:
                raise RuntimeError(
                    f"ffmpeg exited {proc.returncode}:\n{proc.stderr[-3000:]}"
                )

            output_size = output_path.stat().st_size
            print(
                f"[render] output: {output_size / 1e6:.1f} MB → "
                f"projects/{project_id}/stories/{story_id}/output.mp4"
            )

            # 5. Upload output MP4 to R2
            output_key = f"projects/{project_id}/stories/{story_id}/output.mp4"
            with output_path.open("rb") as f:
                r2.put_object(
                    Bucket=bucket,
                    Key=output_key,
                    Body=f,
                    ContentType="video/mp4",
                )
            print(f"[render] uploaded {output_key}")

            # 6. Mark story done.
            # Note: project status intentionally NOT changed — it stays at
            # 'transcribed' so users can create additional cuts at any time.
            sb.table("stories").update({
                "status": "done",
                "render_r2_key": output_key,
            }).eq("id", story_id).execute()

            print(f"[render] story {story_id} done ✓")

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"[render] error for story {story_id}: {msg}")
        sb.table("stories").update({
            "status": "error",
            "error_message": msg[:500],
        }).eq("id", story_id).execute()


