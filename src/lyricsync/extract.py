"""Audio extraction stage.

Shells out to ``ffmpeg`` to produce a 16kHz mono WAV from any video
ffmpeg can read. That's the format WhisperX's align() expects.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


class FFmpegMissingError(RuntimeError):
    pass


def require_ffmpeg() -> str:
    path = shutil.which("ffmpeg")
    if path is None:
        raise FFmpegMissingError(
            "ffmpeg not found on PATH. Install via `brew install ffmpeg` "
            "(macOS) or your distro's package manager."
        )
    return path


def extract_audio(video: Path, out_wav: Path) -> Path:
    """Write a 16kHz mono 16-bit PCM wav from ``video``.

    Overwrites the destination. Returns the output path for chaining.
    """
    ffmpeg = require_ffmpeg()
    out_wav.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-y",
        "-i",
        str(video),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-f",
        "wav",
        "-acodec",
        "pcm_s16le",
        str(out_wav),
    ]
    subprocess.run(cmd, check=True)
    return out_wav
