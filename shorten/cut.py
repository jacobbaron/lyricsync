# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Cut a video down to a list of {start, end} ranges.

Usage:
    uv run shorten/cut.py path/to/video.mp4 path/to/ranges.json [-o out.mp4]

ranges.json format:
    [
      {"start": 1.2,  "end": 8.4,  "reason": "intro hook"},
      {"start": 14.0, "end": 22.5, "reason": "main point"}
    ]

Times are seconds (floats). `reason` is optional/ignored.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

# Pad each kept range slightly so we don't clip the start/end of words.
PAD_S = 0.08


def build_filter(ranges: list[tuple[float, float]]) -> str:
    parts = []
    for i, (a, b) in enumerate(ranges):
        parts.append(
            f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];"
            f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS[a{i}]"
        )
    concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(ranges)))
    # Force 8-bit yuv420p so QuickTime / Quick Look will render it.
    # iPhone HDR sources are 10-bit BT.2020 HLG; H.264 High 10 won't play
    # in QuickTime. Proper tone-mapping needs zscale (not in stock ffmpeg);
    # this just bit-depth-converts and re-tags as BT.709 — highlights may
    # look slightly bright, but it plays everywhere.
    parts.append(
        f"{concat_in}concat=n={len(ranges)}:v=1:a=1[vc][a];"
        "[vc]format=yuv420p[v]"
    )
    return ";".join(parts)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("video", type=Path)
    ap.add_argument("ranges", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None)
    ap.add_argument("--pad", type=float, default=PAD_S)
    args = ap.parse_args()

    raw = json.loads(args.ranges.read_text())
    ranges = [
        (max(0.0, float(r["start"]) - args.pad), float(r["end"]) + args.pad)
        for r in raw
    ]
    if not ranges:
        print("no ranges", file=sys.stderr)
        return 1

    out = args.output or args.video.with_name(f"{args.video.stem}_short.mp4")
    filt = build_filter(ranges)

    cmd = [
        "ffmpeg", "-y", "-i", str(args.video),
        "-filter_complex", filt,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(out),
    ]
    print(f"cutting {len(ranges)} ranges → {out}")
    subprocess.run(cmd, check=True)
    total = sum(b - a for a, b in ranges)
    print(f"done. kept {total:.1f}s across {len(ranges)} segments")
    return 0


if __name__ == "__main__":
    sys.exit(main())
