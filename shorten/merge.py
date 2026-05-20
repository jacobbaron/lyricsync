# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Merge per-clip transcripts into one time-ordered transcript on a shared
global timeline.

Usage:
    uv run shorten/merge.py clips.json -o merged_dir/

For each clip in clips.json, reads the transcript at
shorten/out/<clip_stem>/transcript_aligned.json (preferred) or
transcript_fillers.json / transcript.json, shifts every word's timestamp
by the clip's global_start, and writes:

  merged_dir/merged.json   flat word list across all clips, sorted by global time
  merged_dir/merged.txt    human-readable interleaved view
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT_BASE = ROOT / "shorten" / "out"

# Preferred order: aligned (phoneme-tight) → fillers (verbose Whisper) → plain.
TRANSCRIPT_CANDIDATES = (
    "transcript_aligned.json",
    "transcript_fillers.json",
    "transcript.json",
)


def fmt_ts(s: float) -> str:
    m, sec = divmod(s, 60)
    return f"{int(m):02d}:{sec:05.2f}"


def load_transcript(clip_stem: str) -> tuple[Path, dict]:
    d = OUT_BASE / clip_stem
    for name in TRANSCRIPT_CANDIDATES:
        p = d / name
        if p.exists():
            return p, json.loads(p.read_text())
    raise FileNotFoundError(
        f"no transcript found in {d}; run transcribe.py (and optionally "
        f"align.py) first."
    )


def words_from(data: dict) -> list[dict]:
    """Normalize: return list of {word, start, end} dicts.

    Both Whisper API and WhisperX produce a top-level 'words' / 'word_segments'
    list when word timestamps are requested. Fall back to flattening segments.
    """
    words = data.get("words")
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
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("clips_json", type=Path)
    ap.add_argument("-o", "--output-dir", type=Path,
                    default=ROOT / "shorten" / "out" / "_merged")
    ap.add_argument("--gap", type=float, default=1.2,
                    help="seconds of silence to insert a paragraph break in .txt")
    args = ap.parse_args()

    clips = json.loads(args.clips_json.read_text())
    args.output_dir.mkdir(parents=True, exist_ok=True)

    all_words: list[dict] = []
    for clip in clips:
        stem = Path(clip["file"]).stem
        offset = float(clip["global_start"])
        src_path, data = load_transcript(stem)
        for w in words_from(data):
            all_words.append({
                "word": w["word"],
                "global_start": w["start"] + offset,
                "global_end": w["end"] + offset,
                "local_start": w["start"],
                "local_end": w["end"],
                "source": clip["name"],
                "source_path": clip["file"],
            })
        print(f"loaded {len(words_from(data)):4d} words from {src_path.name} "
              f"(clip {clip['name']}, offset {offset:.2f}s)")

    all_words.sort(key=lambda w: w["global_start"])

    out_json = args.output_dir / "merged.json"
    out_json.write_text(json.dumps(all_words, indent=2))

    # Human-readable view: build "utterances" PER SOURCE (consecutive words
    # from one source with gaps < args.gap), then sort all utterances by
    # global start. This is the right layout for multi-mic recordings where
    # several sources speak simultaneously — each phone gets its own line
    # in the conversation log instead of being interleaved word-by-word.
    by_source: dict[str, list[dict]] = {}
    for w in all_words:
        by_source.setdefault(w["source"], []).append(w)

    utterances: list[dict] = []
    for src, ws in by_source.items():
        ws.sort(key=lambda x: x["global_start"])
        cur_start = ws[0]["global_start"]
        cur_words: list[str] = []
        last_end = ws[0]["global_start"]
        for w in ws:
            if cur_words and w["global_start"] - last_end > args.gap:
                utterances.append({
                    "source": src,
                    "start": cur_start,
                    "end": last_end,
                    "text": " ".join(cur_words),
                })
                cur_start = w["global_start"]
                cur_words = []
            cur_words.append(w["word"])
            last_end = w["global_end"]
        if cur_words:
            utterances.append({
                "source": src,
                "start": cur_start,
                "end": last_end,
                "text": " ".join(cur_words),
            })

    utterances.sort(key=lambda u: u["start"])

    # Insert blank-line breaks on global-time silence gaps between utterances.
    lines: list[str] = []
    prev_end = 0.0
    BIG_GAP = 5.0
    for u in utterances:
        if lines and u["start"] - prev_end > BIG_GAP:
            lines.append("")
        lines.append(f"[{fmt_ts(u['start'])}] ({u['source']})  {u['text']}")
        prev_end = max(prev_end, u["end"])

    out_txt = args.output_dir / "merged.txt"
    out_txt.write_text("\n".join(lines) + "\n")

    print(f"\n{len(all_words)} words across {len(clips)} clips")
    print(f"  {out_json}")
    print(f"  {out_txt}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
