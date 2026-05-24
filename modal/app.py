"""Modal app — lyricsync background compute tasks.

Deploy:
    modal deploy modal/app.py

Implements:
    P1-06  transcribe_clip          — Whisper word-level transcription
    P1-07  align_and_merge          — WhisperX alignment + global timeline merge
    P1-11  render_story (TODO)      — ffmpeg multi-clip render

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
MODAL_TRANSCRIBE_URL and MODAL_ALIGN_URL in your Vercel environment variables.
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
