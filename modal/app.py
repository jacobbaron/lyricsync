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

# Timeline (EDL) model — schema, validation, edit ops, ffmpeg compiler
# (stdlib only, unit-tested in tests/test_timeline.py).
from timeline import (
    DEFAULT_H,
    DEFAULT_W,
    TimelineError,
    apply_ops,
    choose_canvas,
    compile_timeline,
    expand_clean_speech,
    timeline_duration,
    timeline_from_ranges,
    validate_timeline,
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
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
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


def _load_words(project_id: str) -> list[dict]:
    """Read the project's aligned word list from merged.json in R2.

    Returns [] when the transcript is missing — callers that need word timings
    (clean_speech) should surface a clear error in that case.
    """
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    key = f"projects/{project_id}/merged.json"
    try:
        obj = r2.get_object(Bucket=bucket, Key=key)
    except Exception:
        return []
    merged = json.loads(obj["Body"].read())
    return merged.get("words", []) or []


def _read_clip_audio_analysis(r2, bucket: str, project_id: str, clip_id: str):
    """Read + shape one clip's stored audio_analysis.json from R2.

    Returns {vad_prob, vad_hop, peaks, peaks_hop} or None when the analysis is
    missing or has no VAD curve (in which case clean_speech falls back to
    word-gap timing). The path is per-clip and the same for local and foreign
    clips — only the (project_id, clip_id) lookup differs by caller.
    """
    key = f"projects/{project_id}/clips/{clip_id}/audio_analysis.json"
    try:
        doc = json.loads(r2.get_object(Bucket=bucket, Key=key)["Body"].read())
    except Exception:
        return None
    vad = doc.get("vad") or {}
    wave = doc.get("waveform") or {}
    if not vad.get("prob"):
        return None
    return {
        "vad_prob": vad.get("prob"),
        "vad_hop": vad.get("hop") or 0.032,
        "peaks": wave.get("peaks") or [],
        "peaks_hop": wave.get("hop") or 0.02,
    }


def _clean_speech_targets(timeline: dict, ops: list[dict]) -> list[dict]:
    """The video items a clean_speech op in `ops` targets (by id)."""
    from timeline import video_items

    target_ids = {
        (o or {}).get("id")
        for o in (ops or [])
        if (o or {}).get("op") == "clean_speech"
    }
    if not target_ids:
        return []
    return [it for it in video_items(timeline) if it.get("id") in target_ids]


def _load_audio_by_source(
    project_id: str, timeline: dict, ops: list[dict]
) -> dict:
    """For each LOCAL clip a clean_speech op targets (bare filename, no
    clip_id), load its stored audio analysis so the edit can use the VAD-fused
    silence crop. Cross-project targets are handled by _load_audio_by_clip_id.

    Returns {filename: {vad_prob, vad_hop, peaks, peaks_hop}} for the clips that
    have been analyzed; clips without an analysis are simply absent, and
    apply_ops falls back to word-gap timing for those.
    """
    sources = {
        it.get("source")
        for it in _clean_speech_targets(timeline, ops)
        if it.get("source") and not it.get("clip_id")
    }
    if not sources:
        return {}

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    rows = sb.table("clips").select("id, filename").eq(
        "project_id", project_id
    ).execute()
    id_by_name = {c["filename"]: c["id"] for c in (rows.data or [])}

    out: dict = {}
    for name in sources:
        cid = id_by_name.get(name)
        if not cid:
            continue
        data = _read_clip_audio_analysis(r2, bucket, project_id, cid)
        if data is not None:
            out[name] = data
    return out


def _resolve_clips_global(sb, clip_ids) -> dict:
    """Resolve a set of global clip_ids → {clip_id: {project_id, filename}}.

    Single-user app: any clip_id is resolvable across all projects with no
    permission check (see docs/cross_project_editing.md). Only the rows actually
    referenced are fetched.
    """
    ids = sorted({c for c in (clip_ids or []) if c})
    if not ids:
        return {}
    rows = sb.table("clips").select("id, project_id, filename").in_(
        "id", ids
    ).execute()
    return {
        c["id"]: {"project_id": c["project_id"], "filename": c.get("filename")}
        for c in (rows.data or [])
    }


def _load_words_by_clip_id(timeline: dict, ops: list[dict]) -> dict:
    """Words for each FOREIGN clip a clean_speech op targets, keyed by clip_id.

    Tier 2: a cross-project clean_speech target carries a global `clip_id`. We
    resolve each one's owning project, read THAT project's merged.json, and keep
    only that clip's words (matched by the clip's own filename within its own
    project). Returns {clip_id: [words…]}; absent/unanalyzed clips are simply
    omitted. Resolving per clip_id — not by filename over the home project —
    avoids the filename-collision trap (two clips, same filename, different
    projects). See docs/cross_project_editing.md (Tier 2).
    """
    clip_ids = {
        it.get("clip_id")
        for it in _clean_speech_targets(timeline, ops)
        if it.get("clip_id")
    }
    if not clip_ids:
        return {}

    sb = _supabase()
    resolved = _resolve_clips_global(sb, clip_ids)
    # Load each owning project's merged.json once, then slice per clip filename.
    words_by_project: dict[str, list[dict]] = {}
    out: dict[str, list[dict]] = {}
    for cid, info in resolved.items():
        pid = info["project_id"]
        filename = info.get("filename")
        if pid not in words_by_project:
            words_by_project[pid] = _load_words(pid)
        clip_words = [
            w for w in words_by_project[pid]
            if w.get("source") in (None, filename)
        ]
        out[cid] = clip_words
    return out


def _load_audio_by_clip_id(timeline: dict, ops: list[dict]) -> dict:
    """Audio analysis for each FOREIGN clean_speech target, keyed by clip_id.

    Tier 2: resolve each target's clip_id → (project_id, clip_id) globally and
    read its per-clip audio_analysis.json directly (the path is already
    per-clip; only the lookup was home-scoped). Returns
    {clip_id: {vad_prob, vad_hop, peaks, peaks_hop}}; unanalyzed clips are
    omitted and clean_speech falls back to word-gap timing for those.
    """
    clip_ids = {
        it.get("clip_id")
        for it in _clean_speech_targets(timeline, ops)
        if it.get("clip_id")
    }
    if not clip_ids:
        return {}

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    resolved = _resolve_clips_global(sb, clip_ids)

    out: dict = {}
    for cid, info in resolved.items():
        data = _read_clip_audio_analysis(r2, bucket, info["project_id"], cid)
        if data is not None:
            out[cid] = data
    return out


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
        # Pin pyannote.audio to 3.x for diarization. whisperx pulls 4.x, whose
        # speaker-diarization-3.1 pipeline now requires a *new* gated model
        # (speaker-diarization-community-1). The 3.x pipeline instead uses
        # segmentation-3.0 (gated, already accepted) + an ungated embedding, so
        # it works with the HF access we already have.
        "pip install 'pyannote.audio>=3.1,<4'",
    )
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
    )
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
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
            # WhisperX forced-alignment confidence for this word (0–1); None
            # when unavailable (e.g. Whisper-API output, or unaligned words).
            # Lets downstream cleanup pad/skip low-confidence word boundaries.
            "score": float(w["score"]) if w.get("score") is not None else None,
            # Per-clip speaker label (e.g. "SPEAKER_00"); None if diarization
            # was disabled or failed. See _load_diarizer / _assign_speakers.
            "speaker": w.get("speaker"),
        })
    return out


DIARIZE_MODEL = "pyannote/speaker-diarization-3.1"


def _load_diarizer(hf_token: str | None, device: str = "cpu") -> tuple[object, dict]:
    """Load the pyannote.audio speaker-diarization pipeline once.

    Returns (pipeline_or_None, info) where info is a JSON-serializable diagnostic
    dict (token presence, pyannote version, model, load error). The pipeline is
    None — and the worker proceeds without speaker labels — whenever the token is
    missing or the pipeline can't load. Alignment is the critical path and must
    never break because diarization is unavailable.

    We call pyannote.audio's Pipeline.from_pretrained directly rather than
    whisperx's DiarizationPipeline wrapper: the wrapper's constructor signature
    drifts between whisperx versions (e.g. it dropped the use_auth_token kwarg),
    whereas the pyannote API is stable. pyannote is installed as a whisperx dep.

    Diarization tells us "who spoke when" by clustering voice embeddings into
    turns. It needs a Hugging Face token (HF_TOKEN in lyricsync-secrets) with the
    gated pyannote/speaker-diarization-3.1 + segmentation-3.0 models accepted.
    """
    info: dict = {
        "token_present": bool(hf_token),
        "model": DIARIZE_MODEL,
        "backend": "pyannote.audio",
        "pyannote_version": None,
        "loaded": False,
        "error": None,
    }
    if not hf_token:
        info["error"] = "no HF_TOKEN in env"
        print("[align] diarization disabled — no HF_TOKEN in lyricsync-secrets")
        return None, info
    try:
        import pyannote.audio
        import torch
        from pyannote.audio import Pipeline

        info["pyannote_version"] = getattr(pyannote.audio, "__version__", "?")
        info["torch_version"] = getattr(torch, "__version__", "?")

        # PyTorch 2.6 flipped torch.load's default to weights_only=True, which
        # rejects pyannote 3.x's pickled checkpoints (non-tensor globals like
        # torch.to / TorchVersion) with an UnpicklingError. pytorch-lightning
        # passes weights_only=True *explicitly*, so we must force it back off
        # (not setdefault). We only load official pyannote models pinned by name
        # (DIARIZE_MODEL), so loading the full pickle is safe in this process.
        if not getattr(torch.load, "_lyricsync_full_load", False):
            _orig_torch_load = torch.load

            def _full_torch_load(*args, **kwargs):
                kwargs["weights_only"] = False
                return _orig_torch_load(*args, **kwargs)

            _full_torch_load._lyricsync_full_load = True
            torch.load = _full_torch_load

        print(f"[align] loading pyannote diarization pipeline ({DIARIZE_MODEL})…")
        # The auth kwarg was renamed `use_auth_token` → `token` in pyannote.audio
        # 4.x. Try the current name first, then the legacy one, then no kwarg
        # (pyannote also reads the HF_TOKEN env var, which is set in-container).
        pipe = None
        for kw in ("token", "use_auth_token", None):
            try:
                pipe = (
                    Pipeline.from_pretrained(DIARIZE_MODEL)
                    if kw is None
                    else Pipeline.from_pretrained(DIARIZE_MODEL, **{kw: hf_token})
                )
                info["auth_kwarg"] = kw or "env"
                break
            except TypeError:
                continue  # wrong kwarg name for this version — try the next
        if pipe is None:
            # pyannote returns None (not raises) when the token can't access the
            # gated repo — usually un-accepted model terms or a token lacking
            # gated-repo read scope.
            info["error"] = (
                "Pipeline.from_pretrained returned None — token cannot access the "
                "gated repo (accept the model terms / grant gated-repo read scope)"
            )
            print(f"[align] diarizer load failed (non-fatal): {info['error']}")
            return None, info
        try:
            import torch

            pipe.to(torch.device(device))
        except Exception:  # noqa: BLE001 — stay on default device if .to fails
            pass
        info["loaded"] = True
        return pipe, info
    except Exception as exc:  # noqa: BLE001
        info["error"] = f"{type(exc).__name__}: {exc}"
        print(f"[align] diarizer load failed (non-fatal): {exc}")
        return None, info


def _diarize_turns(
    pipeline: object,
    wav,
    sample_rate: int = 16000,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> list:
    """Run the pyannote pipeline on a mono waveform → list of (start, end, label).

    Takes the already-loaded 16 kHz mono waveform (numpy array from
    whisperx.load_audio) to avoid re-decoding audio and any file-format issues.

    Optional speaker-count constraints are passed straight to pyannote, which
    clusters the speaker embeddings under them: `num_speakers` forces exactly
    that many (cuts the agglomerative dendrogram at k, k-means-style),
    `min_speakers`/`max_speakers` bound the range instead of pyannote's default
    distance threshold (which tends to over-split). Pass None to let pyannote
    decide.
    """
    import torch

    waveform = torch.from_numpy(wav).unsqueeze(0)  # (1, num_samples)
    constraints = {
        k: v
        for k, v in (
            ("num_speakers", num_speakers),
            ("min_speakers", min_speakers),
            ("max_speakers", max_speakers),
        )
        if v is not None
    }
    annotation = pipeline(
        {"waveform": waveform, "sample_rate": sample_rate}, **constraints
    )
    return [
        (float(turn.start), float(turn.end), str(speaker))
        for turn, _, speaker in annotation.itertracks(yield_label=True)
    ]


def _assign_speakers(words: list[dict], diarize_df) -> set[str]:
    """Attach a per-clip 'speaker' label to each word in place; return the labels.

    Matches each word's midpoint to the diarization turn that covers it, falling
    back to the nearest turn when a word lands in a gap. Labels (SPEAKER_00,
    SPEAKER_01, …) are only consistent *within this clip* — making them stable
    across clips (one project-wide voiceprint set) is a later step.
    """
    turns: list[tuple[float, float, str]] = []
    # diarize_df is a pandas DataFrame ([start, end, speaker, …]); fall back to
    # any row-iterable so we don't hard-depend on its exact type.
    rows = diarize_df.itertuples(index=False) if hasattr(diarize_df, "itertuples") else diarize_df
    for row in rows:
        try:
            start = float(getattr(row, "start", row[0]))
            end = float(getattr(row, "end", row[1]))
            speaker = str(getattr(row, "speaker", row[2]))
        except (TypeError, IndexError, ValueError):
            continue
        turns.append((start, end, speaker))
    if not turns:
        return set()

    labels: set[str] = set()
    for w in words:
        if "start" not in w or "end" not in w:
            continue
        mid = (float(w["start"]) + float(w["end"])) / 2.0
        speaker = next((t[2] for t in turns if t[0] <= mid <= t[1]), None)
        if speaker is None:
            speaker = min(
                turns, key=lambda t: min(abs(mid - t[0]), abs(mid - t[1]))
            )[2]
        w["speaker"] = speaker
        labels.add(speaker)
    return labels


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

    # Optional diarization speaker-count constraints (forwarded to pyannote).
    def _opt_int(key: str) -> int | None:
        val = body.get(key)
        if val is None:
            return None
        try:
            return max(1, int(val))
        except (TypeError, ValueError):
            return None

    _align_worker.spawn(
        project_id,
        num_speakers=_opt_int("num_speakers"),
        min_speakers=_opt_int("min_speakers"),
        max_speakers=_opt_int("max_speakers"),
    )
    return JSONResponse({"status": "accepted"})


@app.function(image=align_image, secrets=secrets, timeout=1800)
def _align_worker(
    project_id: str,
    num_speakers: int | None = None,
    min_speakers: int | None = None,
    max_speakers: int | None = None,
) -> None:
    """Alignment + merge worker (P1-07).

    num_speakers / min_speakers / max_speakers, when set, constrain pyannote's
    speaker clustering for every clip in this run (see _diarize_turns).

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

    # Optional speaker diarization ("who spoke when"). Loaded once; None when
    # HF_TOKEN is absent, in which case words simply carry no speaker label.
    # diar_info captures why, surfaced in merged.json for debugging.
    diarizer, diar_info = _load_diarizer(
        os.environ.get("HF_TOKEN") or os.environ.get("HUGGINGFACE_TOKEN"),
        device="cpu",
    )
    diar_info["per_clip"] = []
    diar_info["constraints"] = {
        "num_speakers": num_speakers,
        "min_speakers": min_speakers,
        "max_speakers": max_speakers,
    }

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

                # Speaker diarization: assign a per-clip speaker label to each
                # aligned word in place. Best-effort — failures must not abort
                # the clip (alignment is the deliverable, speakers are a bonus).
                if diarizer is not None:
                    clip_diag: dict = {"source": filename}
                    try:
                        turns = _diarize_turns(
                            diarizer, wav,
                            num_speakers=num_speakers,
                            min_speakers=min_speakers,
                            max_speakers=max_speakers,
                        )
                        clip_diag["turns"] = len(turns)
                        labels = _assign_speakers(
                            aligned.get("word_segments", []), turns
                        )
                        clip_diag["speakers"] = sorted(labels)
                        print(
                            f"[align]    diarized → {len(labels)} speaker(s): "
                            f"{sorted(labels)}"
                        )
                    except Exception as dia_exc:  # noqa: BLE001
                        clip_diag["error"] = f"{type(dia_exc).__name__}: {dia_exc}"
                        print(f"[align]    diarization failed (non-fatal): {dia_exc}")
                    diar_info["per_clip"].append(clip_diag)

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
                        # Forced-alignment confidence (0–1) or None — see
                        # _words_from; used to gauge how tightly a word
                        # boundary can be trusted when cleaning up dead air.
                        "score": w.get("score"),
                        "source": cr["filename"],
                        "source_path": cr["r2_key"],
                        # Per-clip speaker label; (source, speaker) together
                        # identify a speaker turn — labels are NOT yet shared
                        # across clips.
                        "speaker": w.get("speaker"),
                    })
            all_words.sort(key=lambda w: w["global_start"])

            merged_key = f"projects/{project_id}/merged.json"
            r2.put_object(
                Bucket=bucket,
                Key=merged_key,
                Body=json.dumps(
                    {"words": all_words, "diarization": diar_info}
                ).encode(),
                ContentType="application/json",
            )
            print(
                f"[align] wrote {merged_key} with {len(all_words)} words; "
                f"diarization={json.dumps(diar_info)[:500]}"
            )

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

            # 5. Auto-run the canonical visual analysis for each clip so story
            # generation has visual context by the time it runs (roadmap 1.1).
            # Best-effort: alignment success must not depend on this.
            for cr in clip_results:
                try:
                    existing = sb.table("visual_analyses").select("id").eq(
                        "clip_id", cr["clip_id"]
                    ).eq("variant", CANONICAL_VISUAL_VARIANT).in_(
                        "status", ["analyzing", "done"]
                    ).limit(1).execute()
                    if existing.data:
                        continue
                    ins = sb.table("visual_analyses").insert({
                        "clip_id": cr["clip_id"],
                        "variant": CANONICAL_VISUAL_VARIANT,
                        "status": "analyzing",
                    }).execute()
                    _analyze_worker.spawn(ins.data[0]["id"])
                    print(f"[align] spawned visual analysis for {cr['clip_id']}")
                except Exception as va_exc:  # noqa: BLE001
                    print(
                        f"[align] visual analysis spawn failed (non-fatal) "
                        f"for {cr['clip_id']}: {va_exc}"
                    )

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
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
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
- The source must exactly match a filename from the transcript headers

Some sections may include visual annotations from an automated analysis of the
footage:
- "[visual context: …]" — a one-line summary of what that clip shows on screen
- "[visual 12s — reaction: …]" — a timestamped on-screen moment (a laugh, a
  gesture, an action beat), placed next to the speech said around that time,
  sometimes with facial-expression and vocal-tone reads
Use them to favor moments with strong visual energy — a genuine reaction, a
reveal, an action — not just strong words. They are annotations, NOT speech:
never include bracketed [visual …] lines in a quote. Quotes must be verbatim
spoken words only.\
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
    # The transcript block dwarfs everything else in every round's context and
    # is identical across rounds — mark it cacheable so multi-round iteration
    # only pays full input cost once per cache window.
    messages.append({
        "role": "user",
        "content": [{
            "type": "text",
            "text": first_content,
            "cache_control": {"type": "ephemeral"},
        }],
    })

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

        # 1b. Load per-clip visual tracks (roadmap 1.1) — best-effort: story
        # generation must never fail because visual analysis is missing, still
        # running, or errored. Prefer the canonical variant; otherwise use the
        # newest completed analysis of any variant.
        visuals_by_source: dict[str, dict] = {}
        try:
            clips_res = sb.table("clips").select(
                "id, filename"
            ).eq("project_id", project_id).execute()
            id_to_name = {
                c["id"]: c["filename"] for c in (clips_res.data or [])
            }
            if id_to_name:
                va = sb.table("visual_analyses").select(
                    "clip_id, variant, result"
                ).in_("clip_id", list(id_to_name)).eq(
                    "status", "done"
                ).order("created_at", desc=True).execute()
                chosen: dict[str, dict] = {}
                for row in va.data or []:  # newest first
                    cid = row["clip_id"]
                    have = chosen.get(cid)
                    if have is not None and (
                        have["variant"] == CANONICAL_VISUAL_VARIANT
                        or row["variant"] != CANONICAL_VISUAL_VARIANT
                    ):
                        continue
                    chosen[cid] = row
                for cid, row in chosen.items():
                    if row.get("result"):
                        visuals_by_source[id_to_name[cid]] = row["result"]
        except Exception as v_exc:  # noqa: BLE001
            print(f"[generate] visual context unavailable (non-fatal): {v_exc}")

        transcript_text = format_transcript(words, visuals_by_source or None)
        print(
            f"[generate] visual context present for "
            f"{len(visuals_by_source)}/{len(set(w.get('source') for w in words))} "
            f"source clip(s)"
        )

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

        # Record the exact perception document this round saw, for later
        # eval/regression comparison (roadmap 1.1 step 4). Non-fatal.
        try:
            sb.table("generation_rounds").update({"debug": {
                "perception_doc": transcript_text,
                "visual_sources": sorted(visuals_by_source),
                "word_count": len(words),
            }}).eq("id", round_id).execute()
        except Exception as d_exc:  # noqa: BLE001
            print(f"[generate] round debug write failed (non-fatal): {d_exc}")

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
    # transcript.py + timeline.py must be present because app.py imports them at
    # the module level; Modal's cloudpickle serialization captures those globals
    # and the container crashes on startup if a module can't be imported.
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
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
    # PERCEPTION T3: same as with_transcript, but also grounded on the cheap
    # deterministic signals (T1 quality + T2 camera_motion) — the prompt gets a
    # ground-truth block of when the camera moves / where the cuts are / which
    # spans are unusable. Run alongside with_transcript to A/B grounded vs
    # ungrounded before making grounding the default.
    "grounded": {
        "model": "gemini-3.5-flash",
        "strategy": "transcript",
        "media_resolution": "MEDIA_RESOLUTION_HIGH",
        "fps": 3,
        "needs_transcript": True,
        "use_signals": True,
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

# The variant that feeds story generation (roadmap 1.1). Auto-run for every
# clip after alignment (it needs the aligned transcript as ground truth);
# _generate_worker prefers this variant's track when building the perception
# document, falling back to the newest completed analysis of any variant.
CANONICAL_VISUAL_VARIANT = "with_transcript"


def _set_analysis(sb, analysis_id: str, **fields) -> None:
    sb.table("visual_analyses").update(fields).eq("id", analysis_id).execute()


def _load_clip_signals(sb, clip_id: str) -> dict:
    """PERCEPTION T3: latest done quality + camera_motion signal results for a
    clip, as `{"quality": <result>, "camera_motion": <result>}`.

    Best-effort and degrades gracefully: a kind with no completed row is simply
    omitted, so grounding falls back to the ungrounded prompt for whatever's
    missing. Each `result` is the compact `clip_signals.result` JSON.
    """
    out: dict = {}
    for kind in ("quality", "camera_motion"):
        try:
            r = (
                sb.table("clip_signals")
                .select("result")
                .eq("clip_id", clip_id)
                .eq("kind", kind)
                .eq("status", "done")
                .order("created_at", desc=True)
                .limit(1)
                .execute()
            )
            if r.data and r.data[0].get("result"):
                out[kind] = r.data[0]["result"]
        except Exception:  # noqa: BLE001 — grounding is best-effort
            pass
    return out


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


def _probe_display_dims(video_path: Path) -> tuple[int, int] | None:
    """Rotation-corrected (display) width/height of a video via ffprobe.

    iPhone clips are often stored as a landscape frame plus a 90/270° display
    matrix; ffmpeg auto-applies that rotation when decoding into the
    filtergraph, so we swap w/h here to match what the scale filter sees. This
    feeds choose_canvas so the render frame matches the footage instead of
    letterboxing it into the default portrait canvas.
    """
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries",
            "stream=width,height:stream_tags=rotate:stream_side_data=rotation",
            "-of", "json", str(video_path),
        ],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return None
    try:
        stream = (json.loads(result.stdout).get("streams") or [{}])[0]
        w, h = int(stream["width"]), int(stream["height"])
    except (ValueError, KeyError, IndexError, json.JSONDecodeError):
        return None
    rotation = 0
    tag = (stream.get("tags") or {}).get("rotate")
    if tag is not None:
        try:
            rotation = int(tag)
        except (ValueError, TypeError):
            pass
    for sd in stream.get("side_data_list") or []:
        if sd.get("rotation") is not None:
            try:
                rotation = int(sd["rotation"])
            except (ValueError, TypeError):
                pass
    if abs(rotation) % 180 == 90:
        w, h = h, w
    return (w, h)


# The Gemini File API rejects uploads larger than 2 GiB (it returns a 413
# FAILED_PRECONDITION "Media is too large. Limit: 2147483648"). A long raw
# session straight off a phone — e.g. a 30-min vocal-tracking take — easily
# exceeds that as a .mov, which kills visual analysis before it starts. When
# the download is too big we transcode it down to a Gemini-friendly H.264 that
# keeps enough visual detail for expression/segment analysis while landing well
# under the cap.
GEMINI_UPLOAD_LIMIT_BYTES = 2 * 1024**3  # hard cap from the File API 413
# Trigger transcoding below the hard cap to leave headroom for muxing slack.
GEMINI_UPLOAD_TARGET_BYTES = 1_800_000_000


def _transcode_for_gemini(src: Path, out: Path) -> Path:
    """Re-encode `src` down to fit under the Gemini File API size cap.

    Fits the frame inside a 1280×1280 box (preserving aspect, even dims, no
    upscaling) and re-encodes H.264 at a modest CRF. Audio is kept — the
    ``with_transcript`` strategy benefits from the soundtrack. Returns `out`;
    raises ``subprocess.CalledProcessError`` if ffmpeg fails.
    """
    cmd = [
        "ffmpeg", "-y", "-i", str(src),
        "-vf",
        "scale=1280:1280:force_original_aspect_ratio=decrease:"
        "force_divisible_by=2",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
        "-c:a", "aac", "-b:a", "128k",
        "-movflags", "+faststart",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)
    return out


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


@app.function(image=analyze_image, secrets=secrets, timeout=1800)
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

            # Gemini's File API caps uploads at 2 GiB; a long raw session blows
            # past that, so shrink it first when needed (otherwise upload as-is).
            if video_path.stat().st_size > GEMINI_UPLOAD_TARGET_BYTES:
                step(
                    f"file is {video_path.stat().st_size / 1e9:.2f} GB — "
                    "transcoding under Gemini's 2 GB upload cap"
                )
                shrunk = tmp / "clip_small.mp4"
                try:
                    _transcode_for_gemini(video_path, shrunk)
                except subprocess.CalledProcessError as ff_exc:
                    detail = ff_exc.stderr[-1500:] if ff_exc.stderr else str(ff_exc)
                    raise RuntimeError(
                        f"transcode for Gemini upload failed: {detail}"
                    ) from ff_exc
                new_size = shrunk.stat().st_size
                debug["transcoded_size_bytes"] = new_size
                step(f"transcoded to {new_size / 1e9:.2f} GB")
                video_path = shrunk

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

            # PERCEPTION T3: ground the prompt on the clip's deterministic
            # quality + camera-motion signals when the variant opts in. Loaded
            # only for grounded variants so existing variants are byte-for-byte
            # unchanged; missing signals degrade to the ungrounded prompt.
            signals = None
            if variant.get("use_signals"):
                signals = _load_clip_signals(sb, clip_id)
                debug["signals"] = signals
                step(
                    "loaded grounding signals: "
                    + ", ".join(sorted(signals)) if signals else "no grounding signals found"
                )

            prompt = build_prompt(
                duration,
                strategy=variant["strategy"],
                transcript_text=transcript_text,
                signals=signals,
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
# PERCEPTION-01: Interactive perception tools (roadmap §1.3)
# ---------------------------------------------------------------------------
#
# On-demand "zoom in" perception for an editing agent: instead of trusting a
# single whole-clip Gemini analysis (coarse on long clips), the agent can pull
# frames, ask Gemini about a specific sub-range, or grab a tiled contact sheet
# of a window — the same way a human editor scrubs.
#
# Each tool is a synchronous Modal endpoint that Vercel calls and *awaits*
# (results are wanted inline, unlike the fire-and-forget analyze worker). The
# web route owns auth (Bearer lsk_…), cache lookup/write (clip_inspections),
# and presigning the R2 keys these return. Outputs are immutable per params and
# cached in R2 under the clip prefix.

# Same base as analyze_image (ffmpeg + genai + boto3 + supabase + helper
# modules) plus the bundled overlay font for burning timestamps into contact
# sheets via drawtext.
perception_image = analyze_image.add_local_file(
    Path(__file__).parent / "assets" / "Montserrat.ttf",
    "/root/overlay_font.ttf",
)


def _perception_clip(sb, clip_id: str) -> dict:
    """Load the (r2_key, project_id, duration_secs) for a clip, or raise 404."""
    row = sb.table("clips").select(
        "id, r2_key, project_id, duration_secs, filename"
    ).eq("id", clip_id).limit(1).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="clip not found")
    clip = row.data[0]
    if not clip.get("r2_key"):
        raise HTTPException(status_code=409, detail="clip has no uploaded video yet")
    return clip


def _download_clip(r2, bucket: str, clip: dict, tmp: Path) -> Path:
    ext = Path(clip["r2_key"]).suffix or ".mp4"
    video_path = tmp / f"clip{ext}"
    r2.download_file(bucket, clip["r2_key"], str(video_path))
    return video_path


def _extract_frame(video_path: Path, t: float, out: Path) -> None:
    """Fast-seek a single frame at time `t` to `out` as a JPEG.

    `-ss` before `-i` is the fast input seek; `-frames:v 1` grabs one frame.
    """
    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{max(t, 0):.3f}", "-i", str(video_path),
        "-frames:v", "1", "-q:v", "3",
        str(out),
    ]
    subprocess.run(cmd, check=True, capture_output=True, text=True)


@app.function(image=perception_image, secrets=secrets, timeout=300)
@modal.fastapi_endpoint(method="POST")
async def perception_frames(request: Request) -> JSONResponse:
    """Extract N frames from a clip and store them in R2.

    Body: {clip_id, t, n, interval}. Extracts `n` frames starting at `t`,
    `interval` seconds apart (fast-seek per frame), uploads each to
    projects/<pid>/clips/<cid>/frames/<t>.jpg, and returns the R2 keys + the
    exact times. The web route presigns the keys.

    Response: {clip_id, frames: [{t, key}]}
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    clip_id = body.get("clip_id")
    if not clip_id:
        raise HTTPException(status_code=400, detail="clip_id required")
    t0 = float(body.get("t") or 0.0)
    n = max(1, min(int(body.get("n") or 1), 30))
    interval = float(body.get("interval") or 1.0)

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    clip = _perception_clip(sb, clip_id)
    project_id = clip["project_id"]
    duration = clip.get("duration_secs")

    frames: list[dict] = []
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        video_path = _download_clip(r2, bucket, clip, tmp)
        if duration is None:
            duration = _probe_duration(video_path)
        for i in range(n):
            t = t0 + i * interval
            if duration is not None and t > float(duration):
                break
            out = tmp / f"f{i}.jpg"
            try:
                _extract_frame(video_path, t, out)
            except subprocess.CalledProcessError as exc:
                detail = (exc.stderr or "")[-800:]
                raise HTTPException(
                    status_code=500, detail=f"ffmpeg frame extract failed: {detail}"
                ) from exc
            key = f"projects/{project_id}/clips/{clip_id}/frames/{t:.3f}.jpg"
            r2.upload_file(
                str(out), bucket, key,
                ExtraArgs={"ContentType": "image/jpeg"},
            )
            frames.append({"t": round(t, 3), "key": key})

    return JSONResponse({"clip_id": clip_id, "frames": frames})


@app.function(image=perception_image, secrets=secrets, timeout=600)
@modal.fastapi_endpoint(method="POST")
async def perception_describe(request: Request) -> JSONResponse:
    """Ask Gemini Flash about a sub-range of a clip.

    Body: {clip_id, start, end, question?}. Trims [start, end] with ffmpeg,
    uploads the short clip to the Gemini File API, and asks `question` (or a
    sensible default describing who's on screen, expressions, and actions).

    Response: {clip_id, start, end, answer, model}
    """
    import time

    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    clip_id = body.get("clip_id")
    if not clip_id:
        raise HTTPException(status_code=400, detail="clip_id required")
    start = float(body.get("start") or 0.0)
    end = float(body.get("end") if body.get("end") is not None else start + 5.0)
    if end <= start:
        raise HTTPException(status_code=400, detail="end must be greater than start")
    question = (body.get("question") or "").strip() or (
        "Describe exactly what is happening in this short clip: who is on "
        "screen, their facial expressions and emotional tone, what they are "
        "doing, the framing/shot type, and anything notable for an editor "
        "choosing whether to use this moment. Be concrete and concise."
    )

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    clip = _perception_clip(sb, clip_id)

    from google import genai

    model = "gemini-3.5-flash"
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        video_path = _download_clip(r2, bucket, clip, tmp)

        # Trim the sub-range. Re-encode (not stream-copy) so the cut is frame
        # accurate and small for the Gemini upload.
        sub = tmp / "sub.mp4"
        cmd = [
            "ffmpeg", "-y",
            "-ss", f"{start:.3f}", "-to", f"{end:.3f}", "-i", str(video_path),
            "-vf", "scale=1280:1280:force_original_aspect_ratio=decrease:"
            "force_divisible_by=2",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "28",
            "-c:a", "aac", "-b:a", "96k", "-movflags", "+faststart",
            str(sub),
        ]
        try:
            subprocess.run(cmd, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "")[-800:]
            raise HTTPException(
                status_code=500, detail=f"ffmpeg trim failed: {detail}"
            ) from exc

        client = genai.Client(api_key=os.environ["GEMINI_API_KEY"])
        gfile = client.files.upload(file=str(sub))
        t0 = time.time()
        while getattr(gfile.state, "name", str(gfile.state)) == "PROCESSING":
            if time.time() - t0 > 120:
                raise HTTPException(status_code=504, detail="Gemini upload timed out")
            time.sleep(2)
            gfile = client.files.get(name=gfile.name)
        state = getattr(gfile.state, "name", str(gfile.state))
        if state != "ACTIVE":
            raise HTTPException(
                status_code=502, detail=f"Gemini file processing ended in state {state}"
            )

        prompt = (
            f"This clip covers seconds {start:.1f}–{end:.1f} of a longer "
            f"video.\n\n{question}"
        )
        try:
            resp = client.models.generate_content(
                model=model, contents=[gfile, prompt]
            )
            answer = (resp.text or "").strip()
        finally:
            try:
                client.files.delete(name=gfile.name)
            except Exception:  # noqa: BLE001
                pass

    return JSONResponse({
        "clip_id": clip_id,
        "start": round(start, 3),
        "end": round(end, 3),
        "answer": answer,
        "model": model,
    })


@app.function(image=perception_image, secrets=secrets, timeout=300)
@modal.fastapi_endpoint(method="POST")
async def perception_contact_sheet(request: Request) -> JSONResponse:
    """Build one tiled contact-sheet image across a clip range.

    Body: {clip_id, start, end, cols, rows} (default 4×4). Samples cols*rows
    frames evenly across [start, end], burns the timestamp into each, and tiles
    them into a single JPEG stored in R2.

    Response: {clip_id, start, end, cols, rows, key}
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    clip_id = body.get("clip_id")
    if not clip_id:
        raise HTTPException(status_code=400, detail="clip_id required")
    cols = max(1, min(int(body.get("cols") or 4), 8))
    rows = max(1, min(int(body.get("rows") or 4), 8))

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]
    clip = _perception_clip(sb, clip_id)
    project_id = clip["project_id"]
    duration = clip.get("duration_secs")

    count = cols * rows
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        video_path = _download_clip(r2, bucket, clip, tmp)
        if duration is None:
            duration = _probe_duration(video_path) or 0.0

        start = float(body.get("start") or 0.0)
        end = float(body.get("end") if body.get("end") is not None else duration)
        if end <= start:
            end = start + max(float(duration) - start, 1.0)
        # Even sampling across [start, end]; place each sample at the midpoint
        # of its cell so the first/last frames aren't exactly on the edges.
        span = end - start
        times = [start + span * (i + 0.5) / count for i in range(count)]

        # Extract + label each thumbnail. drawtext burns the wall-clock time of
        # the sample into the bottom-left of the frame so the agent can map a
        # tile back to a timestamp.
        thumbs: list[Path] = []
        for i, t in enumerate(times):
            raw = tmp / f"r{i}.jpg"
            try:
                _extract_frame(video_path, t, raw)
            except subprocess.CalledProcessError:
                # Past EOF or a bad seek — skip; tiling tolerates fewer inputs.
                continue
            labeled = tmp / f"l{i}.jpg"
            label = f"{t:.1f}s"
            draw = (
                f"scale=480:-2,"
                f"drawtext=fontfile={OVERLAY_FONT}:text='{label}':"
                f"x=8:y=h-th-8:fontsize=28:fontcolor=white:"
                f"box=1:boxcolor=black@0.6:boxborderw=6"
            )
            try:
                subprocess.run(
                    ["ffmpeg", "-y", "-i", str(raw), "-vf", draw, str(labeled)],
                    check=True, capture_output=True, text=True,
                )
                thumbs.append(labeled)
            except subprocess.CalledProcessError:
                thumbs.append(raw)

        if not thumbs:
            raise HTTPException(status_code=500, detail="no frames could be extracted")

        # Tile into one image. The tile filter needs rows*cols inputs to fill
        # the grid; pad the count with the last thumb if some were skipped.
        while len(thumbs) < count:
            thumbs.append(thumbs[-1])
        # Feed the thumbnails as a numbered image sequence so ffmpeg's tile
        # filter can lay them out in one pass.
        seq_dir = tmp / "seq"
        seq_dir.mkdir()
        for i, p in enumerate(thumbs[:count]):
            (seq_dir / f"{i:03d}.jpg").write_bytes(p.read_bytes())

        sheet = tmp / "sheet.jpg"
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-framerate", "1", "-i", str(seq_dir / "%03d.jpg"),
                    "-vf", f"tile={cols}x{rows}:padding=4:margin=4:color=black",
                    "-frames:v", "1", "-q:v", "3",
                    str(sheet),
                ],
                check=True, capture_output=True, text=True,
            )
        except subprocess.CalledProcessError as exc:
            detail = (exc.stderr or "")[-800:]
            raise HTTPException(
                status_code=500, detail=f"ffmpeg tile failed: {detail}"
            ) from exc

        key = (
            f"projects/{project_id}/clips/{clip_id}/contact_sheets/"
            f"{start:.2f}_{end:.2f}_{cols}x{rows}.jpg"
        )
        r2.upload_file(
            str(sheet), bucket, key, ExtraArgs={"ContentType": "image/jpeg"},
        )

    return JSONResponse({
        "clip_id": clip_id,
        "start": round(start, 3),
        "end": round(end, 3),
        "cols": cols,
        "rows": rows,
        "key": key,
    })


# ---------------------------------------------------------------------------
# PERCEPTION T1: technical-quality QC signal (clip_signals, kind='quality')
# ---------------------------------------------------------------------------

quality_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
        "opencv-python-headless>=4.9",
        "numpy>=1.26",
    )
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
    .add_local_file(Path(__file__).parent / "quality.py", "/root/quality.py")
)

QUALITY_SAMPLE_FPS = 3.0


def _set_signal(sb, signal_id: str, **fields) -> None:
    sb.table("clip_signals").update(fields).eq("id", signal_id).execute()


@app.function(image=quality_image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def analyze_quality(request: Request) -> JSONResponse:
    """Vercel calls this to kick off a quality-QC run; spawns the worker and
    returns immediately. Body: {"signal_id": "<uuid>"} — the worker reads the
    clip off the pre-created clip_signals row.
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    signal_id = body.get("signal_id")
    if not signal_id:
        raise HTTPException(status_code=400, detail="signal_id required")

    _quality_worker.spawn(signal_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=quality_image, secrets=secrets, timeout=1800)
def _quality_worker(signal_id: str) -> None:
    """Technical-quality QC worker (PERCEPTION T1).

    1. Load the clip_signals row → clip (r2_key, duration).
    2. Download the clip, sample frames at ~3 fps via ffmpeg.
    3. Per frame: sharpness (variance of Laplacian), exposure (black/white
       histogram fraction), shake (mean abs inter-frame diff) via OpenCV.
    4. Run ffmpeg `blackdetect` for hard black spans.
    5. Hand the raw per-frame numbers to quality.py for bucketing/scoring.
    6. Store the per-second sidecar in R2 + a compact summary on the row.
    """
    import traceback

    import cv2
    import numpy as np
    from quality import build_quality_doc, parse_blackdetect

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    debug: dict = {"steps": []}

    def step(msg: str) -> None:
        print(f"[quality:{signal_id}] {msg}")
        debug["steps"].append(msg)

    row = sb.table("clip_signals").select("id, clip_id").eq("id", signal_id).limit(1).execute()
    if not row.data:
        print(f"[quality] signal {signal_id} not found — skipping")
        return
    clip_id = row.data[0]["clip_id"]

    clip_row = sb.table("clips").select(
        "id, r2_key, project_id, duration_secs"
    ).eq("id", clip_id).limit(1).execute()
    if not clip_row.data or not clip_row.data[0].get("r2_key"):
        _set_signal(sb, signal_id, status="error", error="clip not found or has no video")
        return
    clip = clip_row.data[0]
    project_id = clip["project_id"]

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            video_path = _download_clip(r2, bucket, clip, tmp)
            duration = clip.get("duration_secs") or _probe_duration(video_path)
            if not duration:
                raise RuntimeError("could not determine clip duration")
            step(f"downloaded, duration={duration:.2f}s")

            frames_dir = tmp / "frames"
            frames_dir.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(video_path),
                    "-vf", f"fps={QUALITY_SAMPLE_FPS},scale=320:-1",
                    str(frames_dir / "f%06d.jpg"),
                ],
                check=True, capture_output=True, text=True,
            )
            frame_paths = sorted(frames_dir.glob("f*.jpg"))
            step(f"sampled {len(frame_paths)} frames at {QUALITY_SAMPLE_FPS} fps")

            samples: list[dict] = []
            prev_gray = None
            for i, fp in enumerate(frame_paths):
                img = cv2.imread(str(fp))
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
                black_frac = float(np.mean(gray < 16))
                white_frac = float(np.mean(gray > 239))
                shake = float(np.mean(np.abs(
                    gray.astype(np.int16) - prev_gray.astype(np.int16)
                ))) if prev_gray is not None else 0.0
                prev_gray = gray
                samples.append({
                    "t": i / QUALITY_SAMPLE_FPS,
                    "sharpness": sharpness,
                    "black_frac": black_frac,
                    "white_frac": white_frac,
                    "shake": shake,
                })

            blackdetect = subprocess.run(
                [
                    "ffmpeg", "-i", str(video_path),
                    "-vf", "blackdetect=d=0.5:pic_th=0.98",
                    "-an", "-f", "null", "-",
                ],
                capture_output=True, text=True,
            )
            black_spans = parse_blackdetect(blackdetect.stderr)
            step(f"blackdetect found {len(black_spans)} span(s)")

            doc = build_quality_doc(duration, QUALITY_SAMPLE_FPS, samples, black_spans)

            quality_key = f"projects/{project_id}/clips/{clip_id}/quality.json"
            r2.put_object(
                Bucket=bucket, Key=quality_key,
                Body=json.dumps(doc).encode(), ContentType="application/json",
            )
            step(f"wrote sidecar {quality_key}")

            _set_signal(
                sb, signal_id, status="done",
                result={"summary": doc["summary"], "flagged_spans": doc["flagged_spans"]},
                result_r2_key=quality_key, debug=debug,
            )
            print(f"[quality] clip {clip_id} ✓ {doc['summary']}")
    except Exception as exc:  # noqa: BLE001
        debug["traceback"] = traceback.format_exc()
        _set_signal(sb, signal_id, status="error", error=str(exc), debug=debug)
        print(f"[quality] clip {clip_id} failed: {exc}")


# ---------------------------------------------------------------------------
# PERCEPTION T2: camera-motion / shot-dynamics signal (clip_signals,
# kind='camera_motion'). Reuses the T1 clip_signals table + worker pattern.
# ---------------------------------------------------------------------------

motion_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
        "opencv-python-headless>=4.9",
        "numpy>=1.26",
    )
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
    .add_local_file(Path(__file__).parent / "motion.py", "/root/motion.py")
)

MOTION_SAMPLE_FPS = 3.0
SCENE_THRESHOLD = 0.4  # ffmpeg scene score for a shot boundary


@app.function(image=motion_image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def analyze_motion(request: Request) -> JSONResponse:
    """Vercel calls this to kick off a camera-motion run; spawns the worker and
    returns immediately. Body: {"signal_id": "<uuid>"} — the worker reads the
    clip off the pre-created clip_signals row.
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    signal_id = body.get("signal_id")
    if not signal_id:
        raise HTTPException(status_code=400, detail="signal_id required")

    _motion_worker.spawn(signal_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=motion_image, secrets=secrets, timeout=1800)
def _motion_worker(signal_id: str) -> None:
    """Camera-motion / shot-dynamics worker (PERCEPTION T2).

    1. Load the clip_signals row → clip (r2_key, duration).
    2. Download the clip, sample frames at ~3 fps via ffmpeg.
    3. Per consecutive frame pair: dense Farneback optical flow → global
       translation (dx, dy median), radial divergence (scale = per-frame zoom
       fraction), and mean magnitude (mag).
    4. Run an ffmpeg scene-score pass for in-camera shot boundaries.
    5. Hand the raw per-frame numbers to motion.py for bucketing / labeling.
    6. Store the per-second sidecar in R2 + a compact summary on the row.
    """
    import traceback

    import cv2
    import numpy as np
    from motion import build_motion_doc, parse_scene_cuts

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    debug: dict = {"steps": []}

    def step(msg: str) -> None:
        print(f"[motion:{signal_id}] {msg}")
        debug["steps"].append(msg)

    row = sb.table("clip_signals").select("id, clip_id").eq("id", signal_id).limit(1).execute()
    if not row.data:
        print(f"[motion] signal {signal_id} not found — skipping")
        return
    clip_id = row.data[0]["clip_id"]

    clip_row = sb.table("clips").select(
        "id, r2_key, project_id, duration_secs"
    ).eq("id", clip_id).limit(1).execute()
    if not clip_row.data or not clip_row.data[0].get("r2_key"):
        _set_signal(sb, signal_id, status="error", error="clip not found or has no video")
        return
    clip = clip_row.data[0]
    project_id = clip["project_id"]

    try:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)
            video_path = _download_clip(r2, bucket, clip, tmp)
            duration = clip.get("duration_secs") or _probe_duration(video_path)
            if not duration:
                raise RuntimeError("could not determine clip duration")
            step(f"downloaded, duration={duration:.2f}s")

            frames_dir = tmp / "frames"
            frames_dir.mkdir()
            subprocess.run(
                [
                    "ffmpeg", "-y", "-i", str(video_path),
                    "-vf", f"fps={MOTION_SAMPLE_FPS},scale=320:-1",
                    str(frames_dir / "f%06d.jpg"),
                ],
                check=True, capture_output=True, text=True,
            )
            frame_paths = sorted(frames_dir.glob("f*.jpg"))
            step(f"sampled {len(frame_paths)} frames at {MOTION_SAMPLE_FPS} fps")

            # Precompute the radial-unit grid lazily once the frame size is known
            # (height varies with aspect; width is pinned to 320). `scale` is the
            # mean of the flow's radial component divided by radius → roughly the
            # per-frame fractional zoom (positive = expanding outward = zoom-in).
            radial_x = radial_y = inv_r = mask = None

            samples: list[dict] = []
            prev_gray = None
            for i, fp in enumerate(frame_paths):
                img = cv2.imread(str(fp))
                if img is None:
                    continue
                gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
                if prev_gray is not None:
                    flow = cv2.calcOpticalFlowFarneback(
                        prev_gray, gray, None,
                        0.5, 3, 15, 3, 5, 1.2, 0,
                    )
                    fx, fy = flow[..., 0], flow[..., 1]
                    if radial_x is None:
                        h, w = gray.shape
                        ys, xs = np.mgrid[0:h, 0:w].astype(np.float32)
                        rx = xs - w / 2.0
                        ry = ys - h / 2.0
                        r = np.sqrt(rx * rx + ry * ry)
                        mask = r > 8.0  # skip the center singularity
                        inv_r = np.zeros_like(r)
                        inv_r[mask] = 1.0 / r[mask]
                        radial_x = rx * inv_r
                        radial_y = ry * inv_r
                    radial = (fx * radial_x + fy * radial_y) * inv_r
                    samples.append({
                        "t": i / MOTION_SAMPLE_FPS,
                        "dx": float(np.median(fx)),
                        "dy": float(np.median(fy)),
                        "scale": float(np.mean(radial[mask])) if mask is not None else 0.0,
                        "mag": float(np.mean(np.sqrt(fx * fx + fy * fy))),
                    })
                prev_gray = gray
            step(f"computed optical flow for {len(samples)} frame pairs")

            scene = subprocess.run(
                [
                    "ffmpeg", "-i", str(video_path),
                    "-vf", f"select='gt(scene,{SCENE_THRESHOLD})',showinfo",
                    "-an", "-f", "null", "-",
                ],
                capture_output=True, text=True,
            )
            scene_cuts = parse_scene_cuts(scene.stderr)
            step(f"scene detection found {len(scene_cuts)} cut(s)")

            doc = build_motion_doc(duration, MOTION_SAMPLE_FPS, samples, scene_cuts)

            motion_key = f"projects/{project_id}/clips/{clip_id}/motion.json"
            r2.put_object(
                Bucket=bucket, Key=motion_key,
                Body=json.dumps(doc).encode(), ContentType="application/json",
            )
            step(f"wrote sidecar {motion_key}")

            _set_signal(
                sb, signal_id, status="done",
                result={
                    "summary": doc["summary"],
                    "spans": doc["spans"],
                    "scene_cuts": doc["scene_cuts"],
                },
                result_r2_key=motion_key, debug=debug,
            )
            print(f"[motion] clip {clip_id} ✓ {doc['summary']}")
    except Exception as exc:  # noqa: BLE001
        debug["traceback"] = traceback.format_exc()
        _set_signal(sb, signal_id, status="error", error=str(exc), debug=debug)
        print(f"[motion] clip {clip_id} failed: {exc}")


# ---------------------------------------------------------------------------
# EDL-01: Edit Timeline (synchronous — Vercel proxies the LLM's edit ops here)
# ---------------------------------------------------------------------------

@app.function(image=image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def edit_timeline(request: Request) -> JSONResponse:
    """Apply edit operations to a story's timeline.

    Unlike the other endpoints this runs synchronously — op application is
    pure compute (milliseconds) and the caller needs the result.

    Body:
        story_id          (required)
        ops               list of edit ops (may be empty — an empty list just
                          materializes the timeline from ranges_json)
        base_revision     optional optimistic-concurrency check; 409 on mismatch
        restore_revision  optional — reinstate a prior revision's timeline
                          (mutually exclusive with ops)

    The story's ranges_json is only ever read as the seed for the first
    revision; after that the timeline is the single source of truth.
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    story_id = body.get("story_id")
    if not story_id:
        raise HTTPException(status_code=400, detail="story_id required")
    ops = body.get("ops") or []
    if not isinstance(ops, list):
        raise HTTPException(status_code=400, detail="ops must be an array")
    restore_revision = body.get("restore_revision")
    if restore_revision is not None and ops:
        raise HTTPException(
            status_code=400, detail="pass either ops or restore_revision, not both"
        )

    sb = _supabase()
    row = sb.table("stories").select(
        "id, project_id, ranges_json, timeline_json, timeline_revision"
    ).eq("id", story_id).limit(1).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="story not found")
    story = row.data[0]
    current_rev = story.get("timeline_revision") or 0

    base_revision = body.get("base_revision")
    if base_revision is not None and base_revision != current_rev:
        raise HTTPException(
            status_code=409,
            detail=(
                f"revision conflict: story is at revision {current_rev}, "
                f"you based your edit on {base_revision} — refetch the timeline"
            ),
        )

    try:
        if restore_revision is not None:
            rev_row = sb.table("story_revisions").select("timeline").eq(
                "story_id", story_id
            ).eq("revision", restore_revision).limit(1).execute()
            if not rev_row.data:
                raise HTTPException(
                    status_code=404,
                    detail=f"revision {restore_revision} not found",
                )
            new_timeline = rev_row.data[0]["timeline"]
            errors = validate_timeline(new_timeline)
            if errors:
                raise TimelineError("; ".join(errors))
            ops_record: list[dict] = [
                {"op": "restore", "revision": restore_revision}
            ]
        else:
            base = story.get("timeline_json")
            if base is None:
                ranges = story.get("ranges_json") or []
                if not ranges:
                    raise HTTPException(
                        status_code=400,
                        detail="story has neither a timeline nor ranges",
                    )
                base = timeline_from_ranges(ranges)
            # clean_speech needs the aligned transcript; load it only when used.
            # Home-project words/audio cover local targets (bare filename); the
            # *_by_clip_id maps cover cross-project targets (Tier 2) — each
            # foreign clip's words/audio come from its OWN owning project, keyed
            # by clip_id. See docs/cross_project_editing.md.
            needs_words = any(
                (o or {}).get("op") == "clean_speech" for o in ops
            )
            words = _load_words(story["project_id"]) if needs_words else None
            audio_by_source = (
                _load_audio_by_source(story["project_id"], base, ops)
                if needs_words else None
            )
            words_by_clip_id = (
                _load_words_by_clip_id(base, ops) if needs_words else None
            )
            audio_by_clip_id = (
                _load_audio_by_clip_id(base, ops) if needs_words else None
            )
            new_timeline = apply_ops(
                base, ops, words=words, audio_by_source=audio_by_source,
                words_by_clip_id=words_by_clip_id,
                audio_by_clip_id=audio_by_clip_id,
            )
            ops_record = ops
    except TimelineError as exc:
        # The op/validation message is written for the LLM caller — pass it up.
        raise HTTPException(status_code=400, detail=str(exc))

    new_rev = current_rev + 1
    sb.table("stories").update({
        "timeline_json": new_timeline,
        "timeline_revision": new_rev,
    }).eq("id", story_id).execute()
    sb.table("story_revisions").insert({
        "story_id": story_id,
        "revision": new_rev,
        "ops": ops_record,
        "timeline": new_timeline,
    }).execute()

    return JSONResponse({
        "revision": new_rev,
        "duration_secs": round(timeline_duration(new_timeline), 3),
        "timeline": new_timeline,
    })


@app.function(image=image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def preview_clean_speech(request: Request) -> JSONResponse:
    """Dry-run a `clean_speech` cleanup on one clip item — nothing is saved.

    Lets a caller (human or LLM) see exactly what filler/silence would be cut,
    how much time is saved, and the resulting timeline, then tune the params
    before committing the change via POST /edit {op: clean_speech}.

    Body:
        story_id  (required)
        id        (required) the video item to clean
        params    optional cleanup params (see timeline.SPEECH_CLEANUP_DEFAULTS)

    Returns:
        { item_id, revision, plan: {keep, removed, duration_before,
          duration_after, saved, kept_words, filler_words}, timeline }
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    story_id = body.get("story_id")
    item_id = body.get("id")
    if not story_id or not item_id:
        raise HTTPException(status_code=400, detail="story_id and id required")
    params = body.get("params") or {}

    sb = _supabase()
    row = sb.table("stories").select(
        "id, project_id, ranges_json, timeline_json, timeline_revision"
    ).eq("id", story_id).limit(1).execute()
    if not row.data:
        raise HTTPException(status_code=404, detail="story not found")
    story = row.data[0]

    base = story.get("timeline_json")
    if base is None:
        ranges = story.get("ranges_json") or []
        if not ranges:
            raise HTTPException(
                status_code=400, detail="story has neither a timeline nor ranges"
            )
        base = timeline_from_ranges(ranges)

    # Home-project words/audio for a local target; the *_by_clip_id maps cover a
    # cross-project target (Tier 2) — keyed by the foreign clip's global
    # clip_id, sourced from its own owning project. See cross_project_editing.md.
    probe_ops = [{"op": "clean_speech", "id": item_id}]
    words = _load_words(story["project_id"])
    audio_map = _load_audio_by_source(story["project_id"], base, probe_ops)
    words_by_clip_id = _load_words_by_clip_id(base, probe_ops)
    audio_by_clip_id = _load_audio_by_clip_id(base, probe_ops)

    from timeline import video_items as _vitems
    target = next((it for it in _vitems(base) if it.get("id") == item_id), None)
    cid = (target or {}).get("clip_id")
    if cid and cid in audio_by_clip_id:
        audio = audio_by_clip_id.get(cid)
    else:
        audio = audio_map.get((target or {}).get("source"))
    try:
        new_timeline, plan = expand_clean_speech(
            base, item_id, words, params, audio,
            words_by_clip_id=words_by_clip_id,
        )
    except TimelineError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    return JSONResponse({
        "item_id": item_id,
        "revision": story.get("timeline_revision") or 0,
        "plan": plan,
        "duration_secs": round(timeline_duration(new_timeline), 3),
        "timeline": new_timeline,
    })


# ---------------------------------------------------------------------------
# P1-11: Render Story Task
# ---------------------------------------------------------------------------

# Reuse the base image — ffmpeg, boto3 and supabase are already present.
# No PyTorch or WhisperX needed for rendering.
#
# The ffmpeg filtergraph is built by timeline.compile_timeline; this worker
# only resolves sources, manages the clip cache, and runs the command.


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

    1. Load story from DB. Prefer timeline_json (the editable EDL); fall back
       to converting legacy ranges_json via timeline_from_ranges.
    2. Resolve each clip item's source filename to a clip r2_key.
    3. Reuse cached source clips from the render-cache volume; download any
       missing ones from R2 once and keep them for future renders.
    4. Compile the timeline to an ffmpeg filtergraph (timeline.py) and run it:
         - one seeked input per clip item (frame-accurate + fast)
         - per-item speed, hard cuts or crossfades, text track via drawtext
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
        "id, project_id, ranges_json, timeline_json, status"
    ).eq("id", story_id).limit(1).execute()
    rows = row.data or []

    if not rows:
        print(f"[render] story {story_id} not found — skipping")
        return

    story = rows[0]
    project_id: str = story["project_id"]

    timeline = story.get("timeline_json")
    if timeline is None:
        ranges: list[dict] = story["ranges_json"] or []
        if not ranges:
            sb.table("stories").update({
                "status": "error", "error_message": "story has no ranges",
            }).eq("id", story_id).execute()
            return
        timeline = timeline_from_ranges(ranges)

    # 2. Resolve every clip the timeline references to an r2_key.
    #
    # Two reference styles coexist (see docs/cross_project_editing.md):
    #   • bare `source` filename → resolved within the story's HOME project
    #     (legacy, the only style for single-project cuts);
    #   • global `clip_id` (uuid) → resolved across ALL projects, so one cut can
    #     splice clips from several projects (Tier 1 cross-project cuts).
    # `clip_id`, when present on an item, is authoritative; `source` is then
    # only a human-readable label. r2_key is globally unique, so the download
    # cache below stays keyed by r2_key regardless of how a clip was referenced.
    vclip_items = [
        item
        for track in timeline.get("tracks", [])
        if track.get("type") == "video"
        for item in track.get("items", [])
        if item.get("kind") == "clip"
    ]
    ref_clip_ids = {
        item["clip_id"] for item in vclip_items if item.get("clip_id")
    }
    ref_filenames = {
        item["source"]
        for item in vclip_items
        if not item.get("clip_id") and item.get("source")
    }

    # Foreign/global clips by id (cross-project), then home-project clips by
    # filename. Only the rows the timeline actually references are fetched.
    r2_key_by_clip_id: dict[str, str] = {}
    if ref_clip_ids:
        rows = sb.table("clips").select("id, r2_key, project_id").in_(
            "id", sorted(ref_clip_ids)
        ).execute()
        r2_key_by_clip_id = {
            c["id"]: c["r2_key"] for c in (rows.data or []) if c.get("r2_key")
        }

    r2_key_by_filename: dict[str, str] = {}
    if ref_filenames:
        rows = sb.table("clips").select("filename, r2_key").eq(
            "project_id", project_id
        ).in_("filename", sorted(ref_filenames)).execute()
        # If a filename somehow appears more than once in the home project the
        # last write wins; bare filenames are unique within a project in
        # practice, and cross-project disambiguation is what clip_id is for.
        r2_key_by_filename = {
            c["filename"]: c["r2_key"]
            for c in (rows.data or [])
            if c.get("r2_key")
        }

    def _resolve_r2_key(item: dict) -> str:
        cid = item.get("clip_id")
        if cid:
            key = r2_key_by_clip_id.get(cid)
            if not key:
                raise ValueError(f"clip not found for clip_id: {cid!r}")
            return key
        src = item.get("source")
        key = r2_key_by_filename.get(src)
        if not key:
            raise ValueError(f"source clip not found in project: {src!r}")
        return key

    try:
        errors = validate_timeline(timeline)
        if errors:
            raise ValueError("invalid timeline: " + "; ".join(errors))

        with tempfile.TemporaryDirectory() as tmpdir:
            tmp = Path(tmpdir)

            # 3. Resolve each unique source clip to a local file, reusing the
            # persistent render-cache volume. Only clips not already cached are
            # downloaded from R2; everything else is reused across renders.
            cache_dir = Path(RENDER_CACHE_DIR)
            cache_dir.mkdir(parents=True, exist_ok=True)
            render_cache.reload()  # see clips cached by earlier render runs

            # Unique r2_keys the timeline needs (deduped across both ref styles).
            needed_keys = sorted({
                _resolve_r2_key(item) for item in vclip_items
            })
            local_paths: dict[str, Path] = {}  # r2_key → cached file
            downloaded_any = False
            for r2_key in needed_keys:
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
                local_paths[r2_key] = cached

            # Persist any newly downloaded clips so future renders reuse them.
            if downloaded_any:
                render_cache.commit()

            # 3b. Auto-fit the output canvas to the source footage. The default
            # timeline frame is portrait 1080x1920; clips that are 3:4 / 4:3 /
            # landscape would otherwise be letterboxed with black bars. When the
            # timeline still uses the default frame and every source clip shares
            # an aspect ratio, size the canvas to match so nothing is padded. A
            # non-default canvas (set deliberately in the editor) is respected.
            cur_w = int(timeline.get("width") or DEFAULT_W)
            cur_h = int(timeline.get("height") or DEFAULT_H)
            if (cur_w, cur_h) == (DEFAULT_W, DEFAULT_H):
                dims = [
                    d for d in (
                        _probe_display_dims(p) for p in local_paths.values()
                    ) if d
                ]
                fit_w, fit_h = choose_canvas(dims)
                if (fit_w, fit_h) != (cur_w, cur_h):
                    timeline = {**timeline, "width": fit_w, "height": fit_h}
                    print(f"[render] auto-fit canvas {cur_w}x{cur_h} -> {fit_w}x{fit_h}")

            # 4. Compile the timeline and run ffmpeg. resolve_source receives
            # the whole clip item so cross-project clips resolve by clip_id;
            # the r2_key it maps to is the cache key from step 3.
            compiled = compile_timeline(
                timeline,
                resolve_source=lambda item: str(local_paths[_resolve_r2_key(item)]),
                workdir=str(tmp),
                font_path=OVERLAY_FONT,
            )
            for text_path, content in compiled["text_files"]:
                Path(text_path).write_text(content)

            output_path = tmp / "output.mp4"

            cmd = ["ffmpeg", "-y"]
            for input_args in compiled["inputs"]:
                cmd += input_args
            cmd += [
                "-filter_complex", compiled["filter_complex"],
                "-map", "[vout]", "-map", "[aout]",
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
                f"[render] ffmpeg: {len(compiled['inputs'])} input(s) from "
                f"{len(local_paths)} source clip(s), "
                f"~{compiled['duration']:.1f}s output"
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

            # 6. Mark story done. Write the ACTUAL compiled duration back to
            # estimated_duration_secs so the web list shows the real length
            # (it would otherwise keep the stale create-time estimate, e.g.
            # after a speed/time-lapse edit).
            # Note: project status intentionally NOT changed — it stays at
            # 'transcribed' so users can create additional cuts at any time.
            sb.table("stories").update({
                "status": "done",
                "render_r2_key": output_key,
                "estimated_duration_secs": round(float(compiled["duration"]), 2),
            }).eq("id", story_id).execute()

            print(f"[render] story {story_id} done ✓")

    except Exception as exc:  # noqa: BLE001
        msg = str(exc)
        print(f"[render] error for story {story_id}: {msg}")
        sb.table("stories").update({
            "status": "error",
            "error_message": msg[:500],
        }).eq("id", story_id).execute()



# ---------------------------------------------------------------------------
# Clip audio analysis (waveform + Silero VAD curve) for the clip viz UI
# ---------------------------------------------------------------------------
#
# Produces, per clip: a compressed audio proxy for browser playback, an RMS
# waveform envelope, and a Silero VAD speech-probability curve + intervals.
# All the data shaping lives in audio_analysis.py (pure, unit-tested); this
# worker owns only the fragile parts — ffmpeg decode and ONNX inference — and
# degrades gracefully (prob=None, energy-gated intervals) if VAD can't run.
#
# Silero VAD is permissionless (no HF token / gated repo, unlike diarization);
# the package bundles its own ONNX model. We touch only the stable per-window
# model() primitive and derive intervals with our own tested gate.

audio_viz_image = (
    modal.Image.debian_slim(python_version="3.11")
    .apt_install("ffmpeg")
    .pip_install(
        "fastapi[standard]>=0.115",
        "boto3>=1.34",
        "supabase>=2.10",
        "numpy>=1.26",
        "onnxruntime>=1.17",
        "silero-vad>=5.1",
    )
    # app.py imports transcript + timeline at module level (cloudpickled), so
    # every image that runs an app.py function must mount them.
    .add_local_file(Path(__file__).parent / "transcript.py", "/root/transcript.py")
    .add_local_file(Path(__file__).parent / "timeline.py", "/root/timeline.py")
    # Imported lazily inside the worker — only this image needs it.
    .add_local_file(
        Path(__file__).parent / "audio_analysis.py", "/root/audio_analysis.py"
    )
)


def _silero_prob_curve(audio, sr: int) -> list[float]:
    """Per-window Silero VAD speech probabilities for 16 kHz mono float32 audio.

    Uses only the stable per-window model() call (512-sample windows = 32 ms),
    so we don't depend on higher-level helpers whose signatures drift between
    releases. Raises on any failure; the caller treats that as "no curve".
    """
    import numpy as np
    import torch
    from silero_vad import load_silero_vad

    model = load_silero_vad(onnx=True)
    model.reset_states()
    win = 512  # Silero's required window size at 16 kHz
    probs: list[float] = []
    for i in range(0, len(audio) - win + 1, win):
        chunk = torch.from_numpy(np.ascontiguousarray(audio[i:i + win])).unsqueeze(0)
        with torch.no_grad():
            probs.append(float(model(chunk, sr).item()))
    return probs


@app.function(image=audio_viz_image, secrets=secrets, timeout=60)
@modal.fastapi_endpoint(method="POST")
async def analyze_clip_audio(request: Request) -> JSONResponse:
    """Vercel calls this to (re)build a clip's audio proxy + VAD analysis.

    Spawns the worker and returns immediately. Body: {clip_id}.
    """
    secret = request.headers.get("x-webhook-secret", "")
    expected = os.environ.get("MODAL_WEBHOOK_SECRET", "")
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")

    body = await request.json()
    clip_id = body.get("clip_id")
    if not clip_id:
        raise HTTPException(status_code=400, detail="clip_id required")

    _analyze_clip_audio_worker.spawn(clip_id)
    return JSONResponse({"status": "accepted"})


@app.function(image=audio_viz_image, secrets=secrets, timeout=600)
def _analyze_clip_audio_worker(clip_id: str) -> None:
    """Extract audio proxy + waveform + VAD curve for one clip; store in R2.

    Outputs (R2):
      projects/<pid>/clips/<cid>/audio.m4a            playback proxy
      projects/<pid>/clips/<cid>/audio_analysis.json  see audio_analysis.py
    """
    import subprocess
    import wave

    import numpy as np
    from audio_analysis import build_analysis, intervals_from_curve, words_in_clip

    sb = _supabase()
    r2 = _r2()
    bucket = os.environ["R2_BUCKET_NAME"]

    row = sb.table("clips").select(
        "id, project_id, r2_key, filename, duration_secs"
    ).eq("id", clip_id).limit(1).execute()
    if not row.data:
        print(f"[audio] clip {clip_id} not found — skipping")
        return
    clip = row.data[0]
    pid = clip["project_id"]
    filename = clip["filename"]

    words = words_in_clip(_load_words(pid), filename)

    with tempfile.TemporaryDirectory() as td:
        tdp = Path(td)
        src = tdp / "source"
        wav = tdp / "audio16k.wav"
        m4a = tdp / "audio.m4a"
        r2.download_file(bucket, clip["r2_key"], str(src))

        # 16 kHz mono PCM for VAD; AAC mono proxy for browser playback.
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-ar", "16000",
             str(wav)], check=True, capture_output=True,
        )
        subprocess.run(
            ["ffmpeg", "-y", "-i", str(src), "-vn", "-ac", "1", "-b:a", "64k",
             str(m4a)], check=True, capture_output=True,
        )

        with wave.open(str(wav), "rb") as wf:
            sr = wf.getframerate()
            audio = (
                np.frombuffer(wf.readframes(wf.getnframes()), dtype=np.int16)
                .astype(np.float32) / 32768.0
            )
        duration = (len(audio) / sr) if sr else float(clip.get("duration_secs") or 0.0)

        # Waveform: peak amplitude per 20 ms hop, normalized to [0, 1].
        wf_hop = 0.02
        hop_n = max(1, int(sr * wf_hop))
        peaks = [
            float(np.max(np.abs(audio[i:i + hop_n]))) if i < len(audio) else 0.0
            for i in range(0, len(audio), hop_n)
        ]
        mx = max(peaks) if peaks else 0.0
        peaks = [round(p / mx, 4) for p in peaks] if mx > 0 else peaks

        # VAD curve (best effort) → intervals via our tested gate.
        vad_hop = 512 / 16000  # 0.032 s
        try:
            prob = _silero_prob_curve(audio, sr)
        except Exception as exc:  # noqa: BLE001
            print(f"[audio] silero VAD unavailable, falling back to energy gate: {exc}")
            prob = None

        if prob:
            intervals = intervals_from_curve(
                prob, vad_hop, threshold=0.5, min_speech=0.1,
                min_silence=0.1, pad=0.0, total=duration,
            )
        else:
            # Last-resort: gate the waveform energy so the UI still shows speech
            # regions (coarser, but no model needed).
            gate = [1.0 if p > 0.08 else 0.0 for p in peaks]
            intervals = intervals_from_curve(
                gate, wf_hop, threshold=0.5, min_speech=0.15,
                min_silence=0.2, pad=0.0, total=duration,
            )

        audio_key = f"projects/{pid}/clips/{clip_id}/audio.m4a"
        analysis_key = f"projects/{pid}/clips/{clip_id}/audio_analysis.json"
        r2.upload_file(
            str(m4a), bucket, audio_key,
            ExtraArgs={"ContentType": "audio/mp4"},
        )
        doc = build_analysis(
            duration, audio_key, wf_hop, peaks, vad_hop, prob, intervals, words,
        )
        r2.put_object(
            Bucket=bucket, Key=analysis_key,
            Body=json.dumps(doc).encode(), ContentType="application/json",
        )
        print(
            f"[audio] clip {clip_id} ✓ {len(peaks)} peaks, "
            f"prob={'yes' if prob else 'energy-gate'}, "
            f"{len(intervals)} speech intervals, {len(words)} words"
        )
