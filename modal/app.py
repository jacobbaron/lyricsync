"""Modal app — lyricsync background compute tasks.

Deploy:
    modal deploy modal/app.py

Implements:
    P1-06  transcribe_clip          — Whisper word-level transcription
    P1-07  align_and_merge          — WhisperX alignment + global timeline merge
    P1-11  render_story             — ffmpeg multi-clip render

Secrets:
    Create a Modal secret named "lyricsync-secrets" containing:
        SUPABASE_URL
        SUPABASE_SERVICE_ROLE_KEY
        CLOUDFLARE_R2_ACCESS_KEY_ID
        CLOUDFLARE_R2_SECRET_ACCESS_KEY
        R2_BUCKET_NAME
        R2_ENDPOINT              # e.g. https://<account-id>.r2.cloudflarestorage.com
        OPENAI_API_KEY
        MODAL_WEBHOOK_SECRET     # shared with Vercel

After deploying, note the web endpoint URLs printed by Modal and set them as
MODAL_TRANSCRIBE_URL, MODAL_ALIGN_URL, and MODAL_RENDER_URL in your Vercel
environment variables.
"""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
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
)

secrets = [modal.Secret.from_name("lyricsync-secrets")]

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

    # Alternate assistant/user turns for each previous round
    for rnd in prev_rounds:
        # Load that round's stories to reconstruct the assistant's reply
        result = sb.table("stories").select(
            "title, description, estimated_duration_secs, ranges_json"
        ).eq("generation_round_id", rnd["id"]).order("created_at").execute()
        round_stories = [
            {
                "title": s["title"],
                "description": s["description"],
                "estimated_duration_secs": s["estimated_duration_secs"],
                "ranges": s["ranges_json"],
            }
            for s in (result.data or [])
            if s["title"]  # skip placeholder rows that never got filled
        ]
        if round_stories:
            messages.append({
                "role": "assistant",
                "content": (
                    "Here are my suggestions:\n\n"
                    + stories_as_text(round_stories)
                ),
            })

        # The next user turn is the following round's prompt (if any) —
        # but only append if there's a subsequent round after this one.
        # The current round's prompt is added below.
        next_prompt = current_round["prompt"] if rnd is prev_rounds[-1] else None
        if next_prompt or rnd is not prev_rounds[-1]:
            # Find next round's prompt from the list
            idx = prev_rounds.index(rnd)
            if idx + 1 < len(prev_rounds):
                followup_prompt = prev_rounds[idx + 1]["prompt"] or "Generate 3 new options."
            else:
                followup_prompt = current_round["prompt"] or "Generate 3 new options."
            messages.append({
                "role": "user",
                "content": f"{followup_prompt}\n\nPropose 3 new story options.",
            })

    # If there were no previous rounds, the first message already covers round 1.
    # If there were previous rounds, the loop above added the current prompt as
    # the last user turn. Either way we're done.
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


@app.function(image=gen_image, secrets=secrets, timeout=900)
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
        round_row = sb.table("generation_rounds").select(
            "id, round, prompt"
        ).eq("id", round_id).maybe_single().execute()
        if not round_row.data:
            raise ValueError(f"generation round {round_id} not found")
        current_round = round_row.data
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
            ranges = resolve_segments(story_data["segments"], index_by_source)
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
        sb.table("projects").update({
            "status": "error",
            "error_message": msg[:500],
        }).eq("id", project_id).execute()


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


@app.function(image=image, secrets=secrets, timeout=1800)
def _render_worker(story_id: str) -> None:
    """Render worker (P1-11).

    1. Load story from DB (ranges_json, project_id).
    2. Resolve each range's source filename to a clip r2_key.
    3. Download only the unique source clips referenced in ranges.
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
    ).eq("id", story_id).maybe_single().execute()

    if not row.data:
        print(f"[render] story {story_id} not found — skipping")
        return

    story = row.data
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

            # 3. Download each unique source clip exactly once.
            # inputs: r2_key → (local_path, ffmpeg_input_index)
            inputs: dict[str, tuple[Path, int]] = {}
            for rng in ranges:
                source: str = rng["source"]
                r2_key = clip_map.get(source)
                if not r2_key:
                    raise ValueError(f"source clip not found in project: {source!r}")
                if r2_key not in inputs:
                    ext = Path(r2_key).suffix or ".mp4"
                    local_path = tmp / f"clip_{len(inputs)}{ext}"
                    print(f"[render] downloading {r2_key}")
                    r2.download_file(bucket, r2_key, str(local_path))
                    inputs[r2_key] = (local_path, len(inputs))

            # 4. Build ffmpeg filter_complex (mirrors shorten/splice.py exactly).
            W, H = _RENDER_W, _RENDER_H
            FPS, SR = _RENDER_FPS, _RENDER_SR
            vnorm = (
                f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
                f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
                f"setsar=1,fps={FPS}"
            )

            items = []
            for rng in ranges:
                r2_key = clip_map[rng["source"]]
                _, input_idx = inputs[r2_key]
                start = max(0.0, float(rng["start"]) - _RENDER_PAD_S)
                end = float(rng["end"]) + _RENDER_PAD_S
                items.append({"input_idx": input_idx, "start": start, "end": end})

            parts = []
            for i, it in enumerate(items):
                inp = it["input_idx"]
                a, b = it["start"], it["end"]
                parts.append(
                    f"[{inp}:v]trim=start={a}:end={b},setpts=PTS-STARTPTS,"
                    f"{vnorm}[v{i}];"
                    f"[{inp}:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,"
                    f"aresample={SR},aformat=channel_layouts=stereo[a{i}]"
                )
            concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(items)))
            parts.append(
                f"{concat_in}concat=n={len(items)}:v=1:a=1[vc][a];"
                "[vc]format=yuv420p[v]"
            )
            filter_complex = ";".join(parts)

            output_path = tmp / "output.mp4"

            # Build command: inputs in insertion order = ascending input_idx
            cmd = ["ffmpeg", "-y"]
            for local_path, _ in inputs.values():
                cmd += ["-i", str(local_path)]
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
                f"[render] ffmpeg: {len(items)} segment(s) from "
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
