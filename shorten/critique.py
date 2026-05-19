# /// script
# requires-python = ">=3.11"
# dependencies = ["openai>=1.40", "python-dotenv>=1.0"]
# ///
"""Send a cut video's audio to GPT-4o-audio for critique.

Usage:
    uv run shorten/critique.py path/to/short.mp4
"""
from __future__ import annotations

import base64
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from dotenv import load_dotenv
from openai import OpenAI

ROOT = Path(__file__).resolve().parent.parent

PROMPT = """You're reviewing an edited short-form video for quality issues.
Listen carefully and report specific problems you hear, with approximate
timestamps (mm:ss). Look for:

  - Mid-word cuts or clipped syllables
  - Abrupt audio splices (jumpy/unnatural transitions)
  - Lost context that makes a sentence confusing
  - Filler words still present (um, uh, filler "like")
  - Anywhere the cut feels too tight or too loose

Then give an overall verdict: does the edit hold together? What's the single
biggest thing to fix? Be concrete and brief — no generic praise."""


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: critique.py <video>", file=sys.stderr)
        return 2

    video = Path(sys.argv[1]).resolve()
    load_dotenv(ROOT / ".env")
    if not os.getenv("OPENAI_API_KEY"):
        print("OPENAI_API_KEY not set", file=sys.stderr)
        return 1

    with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as f:
        wav = Path(f.name)
    subprocess.run(
        ["ffmpeg", "-y", "-i", str(video), "-vn", "-ac", "1",
         "-ar", "16000", "-c:a", "pcm_s16le", str(wav)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    audio_b64 = base64.b64encode(wav.read_bytes()).decode()
    wav.unlink()

    print("sending to gpt-4o-audio-preview…\n")
    client = OpenAI()
    resp = client.chat.completions.create(
        model="gpt-4o-audio-preview",
        modalities=["text"],
        messages=[{
            "role": "user",
            "content": [
                {"type": "text", "text": PROMPT},
                {"type": "input_audio",
                 "input_audio": {"data": audio_b64, "format": "wav"}},
            ],
        }],
    )
    print(resp.choices[0].message.content)
    return 0


if __name__ == "__main__":
    sys.exit(main())
