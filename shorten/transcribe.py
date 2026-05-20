# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.40", "python-dotenv>=1.0"]
# ///
"""Transcribe a video with word-level timestamps via OpenAI Whisper.

Usage:
    uv run shorten/transcribe.py path/to/video.mp4 [--keep-fillers]
                                                   [--chunk-minutes N]

Outputs (in shorten/out/<stem>/):
    transcript.json  — full Whisper response with word timings
    transcript.txt   — readable transcript with [mm:ss] markers per segment

Long audio (> ~20 min) is automatically chunked and the per-chunk
transcripts are merged with proper time offsets. Use --chunk-minutes to
override the chunk length.

--keep-fillers seeds Whisper with a prompt full of "um, uh, like, you know"
so it transcribes disfluencies instead of silently dropping them.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "shorten" / "out"
# Whisper API has a 25MB upload limit. 64 kbps mono mp3 ≈ 0.48 MB/min,
# so ~50 min fits in one shot. We chunk well before that for accuracy
# (Whisper drifts on long audio) and headroom.
AUDIO_BITRATE = "64k"
DEFAULT_CHUNK_MIN = 15.0
AUTO_CHUNK_MIN = 20.0  # auto-chunk if total audio is longer than this

FILLER_PROMPT = (
    "Umm, uh, you know, like, I mean... uh, so, um, like, yeah, "
    "uh-huh, hmm. Include all filler words and disfluencies verbatim."
)


def ffprobe_duration(path: Path) -> float:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(path)],
        check=True, capture_output=True, text=True,
    )
    return float(out.stdout.strip())


def extract_audio(video: Path, dest: Path, start: float | None = None,
                  duration: float | None = None) -> None:
    cmd = ["ffmpeg", "-y"]
    if start is not None:
        cmd += ["-ss", f"{start:.3f}"]
    cmd += ["-i", str(video)]
    if duration is not None:
        cmd += ["-t", f"{duration:.3f}"]
    cmd += ["-vn", "-ac", "1", "-ar", "16000", "-b:a", AUDIO_BITRATE, str(dest)]
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)


def fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def whisper_transcribe(client: OpenAI, audio: Path, prompt: str | None) -> dict:
    kwargs = dict(
        model="whisper-1",
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
    )
    if prompt:
        kwargs["prompt"] = prompt
    with audio.open("rb") as f:
        resp = client.audio.transcriptions.create(file=f, **kwargs)
    return resp.model_dump()


def shift_times(data: dict, offset: float) -> dict:
    """Return a copy of a Whisper response with all timestamps shifted."""
    for seg in data.get("segments", []) or []:
        seg["start"] = float(seg.get("start", 0.0)) + offset
        seg["end"] = float(seg.get("end", 0.0)) + offset
        for w in seg.get("words", []) or []:
            if "start" in w:
                w["start"] = float(w["start"]) + offset
            if "end" in w:
                w["end"] = float(w["end"]) + offset
    for w in data.get("words", []) or []:
        if "start" in w:
            w["start"] = float(w["start"]) + offset
        if "end" in w:
            w["end"] = float(w["end"]) + offset
    return data


def merge_responses(responses: list[dict]) -> dict:
    """Concatenate already-time-shifted Whisper responses."""
    merged: dict = {
        "task": "transcribe",
        "language": responses[0].get("language") if responses else "en",
        "segments": [],
        "words": [],
        "text": "",
    }
    texts = []
    for r in responses:
        merged["segments"].extend(r.get("segments", []) or [])
        merged["words"].extend(r.get("words", []) or [])
        texts.append((r.get("text") or "").strip())
    merged["text"] = " ".join(t for t in texts if t)
    return merged


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--keep-fillers", action="store_true",
                    help="prompt Whisper to keep um/uh/like disfluencies")
    ap.add_argument("--chunk-minutes", type=float, default=DEFAULT_CHUNK_MIN,
                    help=f"chunk length in minutes (default {DEFAULT_CHUNK_MIN})")
    args = ap.parse_args()

    video = args.video.resolve()
    if not video.exists():
        print(f"not found: {video}", file=sys.stderr)
        return 1

    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set (put it in .env)", file=sys.stderr)
        return 1

    out_dir = OUT_DIR / video.stem
    out_dir.mkdir(parents=True, exist_ok=True)

    duration = ffprobe_duration(video)
    chunk_sec = args.chunk_minutes * 60.0
    needs_chunking = duration > AUTO_CHUNK_MIN * 60.0
    suffix = "_fillers" if args.keep_fillers else ""
    prompt = FILLER_PROMPT if args.keep_fillers else None

    client = OpenAI()

    if not needs_chunking:
        audio = out_dir / "audio.mp3"
        print(f"[1/2] extracting audio → {audio.relative_to(ROOT)}")
        extract_audio(video, audio)
        size_mb = audio.stat().st_size / 1_000_000
        print(f"      {size_mb:.1f} MB")
        if size_mb > 25:
            print("audio > 25MB after extraction; re-run with smaller "
                  "--chunk-minutes (forces chunking)", file=sys.stderr)
            return 1
        print("[2/2] sending to Whisper…")
        data = whisper_transcribe(client, audio, prompt)
    else:
        n_chunks = int(duration // chunk_sec) + (1 if duration % chunk_sec else 0)
        print(f"long audio ({duration/60:.1f} min) → {n_chunks} chunks of "
              f"{args.chunk_minutes:.1f} min")
        responses = []
        for i in range(n_chunks):
            start = i * chunk_sec
            this_dur = min(chunk_sec, duration - start)
            chunk_audio = out_dir / f"audio_part{i:02d}.mp3"
            print(f"[{i+1}/{n_chunks}] extracting {start/60:.1f}-"
                  f"{(start+this_dur)/60:.1f} min → {chunk_audio.name}")
            extract_audio(video, chunk_audio, start=start, duration=this_dur)
            size_mb = chunk_audio.stat().st_size / 1_000_000
            print(f"        {size_mb:.1f} MB, transcribing…")
            r = whisper_transcribe(client, chunk_audio, prompt)
            r = shift_times(r, offset=start)
            responses.append(r)
        data = merge_responses(responses)
        # Keep the merged single mp3 too so align.py / critique.py work.
        full_audio = out_dir / "audio.mp3"
        if not full_audio.exists():
            print(f"extracting full-length audio.mp3 for downstream tools…")
            extract_audio(video, full_audio)

    (out_dir / f"transcript{suffix}.json").write_text(json.dumps(data, indent=2))
    lines = [
        f"[{fmt_ts(seg['start'])}] {seg['text'].strip()}"
        for seg in data.get("segments", [])
    ]
    (out_dir / f"transcript{suffix}.txt").write_text("\n".join(lines) + "\n")

    print(f"done → {out_dir.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
