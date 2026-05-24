"""Modal app — lyricsync background compute tasks.

Deploy:
    modal deploy modal/app.py

This file currently implements P1-06 (transcription). P1-07 (alignment +
merge) and P1-11 (render) will be added to the same app.

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

After deploying, note the web endpoint URL printed by Modal and set it as
MODAL_TRANSCRIBE_URL in your Vercel environment variables.
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
