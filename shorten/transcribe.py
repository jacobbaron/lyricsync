# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.40", "python-dotenv>=1.0"]
# ///
"""Transcribe a video with word-level timestamps via OpenAI Whisper.

Usage:
    uv run shorten/transcribe.py path/to/video.mp4 [--keep-fillers]

Outputs (next to the video, in shorten/out/<stem>/):
    transcript.json  — full Whisper response with word timings
    transcript.txt   — readable transcript with [mm:ss] markers per segment

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

FILLER_PROMPT = (
    "Umm, uh, you know, like, I mean... uh, so, um, like, yeah, "
    "uh-huh, hmm. Include all filler words and disfluencies verbatim."
)

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent
OUT_DIR = ROOT / "shorten" / "out"
# Whisper API has a 25MB upload limit. 64 kbps mono mp3 ≈ 0.48 MB/min,
# so ~50 minutes fits. For longer videos we'd need to chunk; not yet.
AUDIO_BITRATE = "64k"


def extract_audio(video: Path, dest: Path) -> None:
    subprocess.run(
        [
            "ffmpeg", "-y", "-i", str(video),
            "-vn", "-ac", "1", "-ar", "16000",
            "-b:a", AUDIO_BITRATE, str(dest),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def fmt_ts(seconds: float) -> str:
    m, s = divmod(int(seconds), 60)
    return f"{m:02d}:{s:02d}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("--keep-fillers", action="store_true",
                    help="prompt Whisper to keep um/uh/like disfluencies")
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
    audio = out_dir / "audio.mp3"

    print(f"[1/2] extracting audio → {audio.relative_to(ROOT)}")
    extract_audio(video, audio)

    size_mb = audio.stat().st_size / 1_000_000
    print(f"      {size_mb:.1f} MB")
    if size_mb > 25:
        print("audio > 25MB; chunking not implemented yet", file=sys.stderr)
        return 1

    print("[2/2] sending to Whisper…")
    client = OpenAI()
    kwargs = dict(
        model="whisper-1",
        response_format="verbose_json",
        timestamp_granularities=["word", "segment"],
    )
    if args.keep_fillers:
        kwargs["prompt"] = FILLER_PROMPT
    with audio.open("rb") as f:
        resp = client.audio.transcriptions.create(file=f, **kwargs)

    data = resp.model_dump()
    suffix = "_fillers" if args.keep_fillers else ""
    (out_dir / f"transcript{suffix}.json").write_text(json.dumps(data, indent=2))

    lines = []
    for seg in data.get("segments", []):
        lines.append(f"[{fmt_ts(seg['start'])}] {seg['text'].strip()}")
    (out_dir / f"transcript{suffix}.txt").write_text("\n".join(lines) + "\n")

    print(f"done → {out_dir.relative_to(ROOT)}/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
