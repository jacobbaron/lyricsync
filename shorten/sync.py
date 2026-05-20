# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Scan a directory of clips and write a clips.json with global offsets.

Usage:
    uv run shorten/sync.py path/to/clips_dir [-o clips.json]

Reads each clip's container creation timestamp via ffprobe, anchors the
earliest one at 0.0, and writes relative offsets to clips.json. You can
hand-edit the offsets in that file before running merge.py.

clips.json shape:
[
  {
    "file": "absolute/path/to/clip.mov",
    "name": "clip.mov",
    "global_start": 0.0,
    "duration": 142.5,
    "creation_time": "2026-05-03T17:42:11.000000Z"
  },
  ...
]
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

VIDEO_EXTS = {".mov", ".mp4", ".m4v", ".mkv", ".webm"}


def ffprobe_json(path: Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json",
         "-show_format", "-show_streams", str(path)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(out.stdout)


def parse_iso(ts: str) -> datetime:
    # ffprobe returns e.g. "2026-05-03T17:42:11.000000Z"
    return datetime.fromisoformat(ts.replace("Z", "+00:00"))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips_dir", type=Path)
    ap.add_argument("-o", "--output", type=Path, default=None,
                    help="default: <clips_dir>/clips.json")
    args = ap.parse_args()

    clips_dir = args.clips_dir.resolve()
    if not clips_dir.is_dir():
        print(f"not a directory: {clips_dir}", file=sys.stderr)
        return 1

    files = sorted(p for p in clips_dir.iterdir()
                   if p.suffix.lower() in VIDEO_EXTS)
    if not files:
        print(f"no video files in {clips_dir}", file=sys.stderr)
        return 1

    entries = []
    for f in files:
        info = ffprobe_json(f)
        fmt = info.get("format", {})
        duration = float(fmt.get("duration", 0.0))
        tags = fmt.get("tags", {}) or {}
        # Prefer the iPhone-embedded capture time, which carries the local
        # timezone offset and is the actual time the recording was made.
        # The plain ``creation_time`` is set when the file is written to
        # whichever device we read it from — for an Apple Photos export,
        # that's the export time, not the capture time.
        ct = (tags.get("com.apple.quicktime.creationdate")
              or tags.get("creation_time"))
        if not ct:
            # Fallback: file mtime. Less reliable but better than nothing.
            ct = datetime.fromtimestamp(
                f.stat().st_mtime, tz=timezone.utc
            ).isoformat()
        entries.append({
            "file": str(f),
            "name": f.name,
            "creation_time": ct,
            "duration": duration,
            "_dt": parse_iso(ct),
        })

    anchor = min(e["_dt"] for e in entries)
    for e in entries:
        e["global_start"] = (e["_dt"] - anchor).total_seconds()
        del e["_dt"]

    entries.sort(key=lambda e: e["global_start"])

    out = args.output or (clips_dir / "clips.json")
    out.write_text(json.dumps(entries, indent=2))

    print(f"{len(entries)} clips → {out}")
    for e in entries:
        print(f"  {e['global_start']:8.2f}s  {e['duration']:6.1f}s  {e['name']}")
    print("\nedit global_start values in clips.json to tweak sync.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
