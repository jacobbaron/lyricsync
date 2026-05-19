"""Re-align a Whisper transcript with WhisperX for tight word timestamps.

Usage:
    uv run python shorten/align.py shorten/out/<stem>/

Reads:
    transcript_fillers.json (or transcript.json if no fillers variant)
    audio.mp3
Writes:
    transcript_aligned.json — same shape but with phoneme-accurate word
    timings under 'words' (and per-segment).
"""
from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: align.py <out_dir>", file=sys.stderr)
        return 2

    out_dir = Path(sys.argv[1]).resolve()
    src = out_dir / "transcript_fillers.json"
    if not src.exists():
        src = out_dir / "transcript.json"
    audio = out_dir / "audio.mp3"
    if not (src.exists() and audio.exists()):
        print(f"missing {src} or {audio}", file=sys.stderr)
        return 1

    print(f"loading {src.name}")
    data = json.loads(src.read_text())
    segments = [
        {"text": s["text"], "start": float(s["start"]), "end": float(s["end"])}
        for s in data["segments"]
    ]

    print("importing whisperx (slow on first call)…")
    import whisperx  # type: ignore

    print(f"loading audio {audio.name}")
    wav = whisperx.load_audio(str(audio))

    print("loading alignment model (en)…")
    model_a, metadata = whisperx.load_align_model(language_code="en", device="cpu")

    print(f"aligning {len(segments)} segments…")
    aligned = whisperx.align(
        segments, model_a, metadata, wav, "cpu", return_char_alignments=False
    )

    out = {
        "language": data.get("language", "en"),
        "duration": data.get("duration"),
        "segments": aligned.get("segments", []),
        "words": aligned.get("word_segments", []),
    }
    dst = out_dir / "transcript_aligned.json"
    dst.write_text(json.dumps(out, indent=2))
    print(f"done → {dst}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
