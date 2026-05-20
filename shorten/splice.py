# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Splice ranges from multiple source clips into one video.

Usage:
    uv run shorten/splice.py ranges.json -o story.mp4

ranges.json shape (one entry per kept segment, in the order they should appear):
[
  {"source": "booth.mov",   "start": 12.40, "end": 15.46},
  {"source": "control.mov", "start":  3.20, "end":  6.10},
  ...
]

`start`/`end` are LOCAL to the source clip (seconds). `source` is either
a bare filename (resolved against clips.json with --clips) or an absolute
path. Clips with mismatched resolutions / frame rates / sample rates are
normalized to a common target (--width / --height / --fps / --sr,
defaulting to 1080×1920 / 30 fps / 48000 Hz) with letterbox padding to
preserve aspect ratio.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

PAD_S = 0.08


def resolve_source(source: str, clip_map: dict[str, str]) -> Path:
    p = Path(source)
    if p.is_absolute() and p.exists():
        return p
    if source in clip_map:
        return Path(clip_map[source])
    # last resort: relative to CWD
    if p.exists():
        return p.resolve()
    raise FileNotFoundError(f"can't locate source clip: {source}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("ranges", type=Path)
    ap.add_argument("-o", "--output", type=Path, required=True)
    ap.add_argument("--clips", type=Path, default=None,
                    help="clips.json from sync.py, to resolve bare filenames")
    ap.add_argument("--pad", type=float, default=PAD_S)
    ap.add_argument("--width", type=int, default=1080)
    ap.add_argument("--height", type=int, default=1920)
    ap.add_argument("--fps", type=int, default=30)
    ap.add_argument("--sr", type=int, default=48000,
                    help="audio sample rate")
    args = ap.parse_args()

    clip_map: dict[str, str] = {}
    if args.clips and args.clips.exists():
        for c in json.loads(args.clips.read_text()):
            clip_map[c["name"]] = c["file"]

    raw = json.loads(args.ranges.read_text())
    if not raw:
        print("no ranges", file=sys.stderr)
        return 1

    # Resolve sources, then assign each distinct source path an ffmpeg input
    # index so we only -i each file once.
    items: list[dict] = []
    inputs: dict[str, int] = {}
    for r in raw:
        src_path = str(resolve_source(r["source"], clip_map))
        if src_path not in inputs:
            inputs[src_path] = len(inputs)
        items.append({
            "input_idx": inputs[src_path],
            "start": max(0.0, float(r["start"]) - args.pad),
            "end": float(r["end"]) + args.pad,
        })

    # Build filter_complex: per-item, trim → scale-and-pad to target
    # resolution → normalize fps + sample rate → concat.
    W, H, FPS, SR = args.width, args.height, args.fps, args.sr
    vnorm = (
        f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
        f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={FPS}"
    )
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

    cmd = ["ffmpeg", "-y"]
    for path in inputs:
        cmd += ["-i", path]
    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-profile:v", "high",
        "-colorspace", "bt709", "-color_primaries", "bt709",
        "-color_trc", "bt709", "-color_range", "tv",
        "-c:a", "aac", "-b:a", "160k",
        "-movflags", "+faststart",
        str(args.output),
    ]

    print(f"splicing {len(items)} ranges from {len(inputs)} sources → {args.output}")
    subprocess.run(cmd, check=True)
    total = sum(it["end"] - it["start"] for it in items)
    print(f"done. {total:.1f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main())
