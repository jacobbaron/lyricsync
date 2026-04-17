"""Canonical ``alignment.json`` serialization (word-level + line grouping).

Line ``start``/``end`` are always derived from the first/last word when
loading so edits cannot drift.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .alignment import AlignedLine, AlignedWord, AlignmentResult

SCHEMA_VERSION = 1


def alignment_to_dict(
    result: AlignmentResult,
    meta: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Serialize ``result`` to a JSON-serializable dict."""
    out: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "lines": [
            {
                "text": line.text,
                "start": line.start,
                "end": line.end,
                "words": [
                    {"text": w.text, "start": w.start, "end": w.end}
                    for w in line.words
                ],
            }
            for line in result.lines
        ],
    }
    if meta:
        out["meta"] = meta
    return out


def alignment_from_dict(data: dict[str, Any]) -> AlignmentResult:
    """Parse dict (e.g. from JSON) into ``AlignmentResult``.

    Recomputes each line's ``start``/``end`` from its words.
    """
    ver = data.get("schema_version")
    if ver != SCHEMA_VERSION:
        raise ValueError(
            f"unsupported alignment schema_version: {ver!r} (expected {SCHEMA_VERSION})"
        )
    raw_lines = data.get("lines")
    if not isinstance(raw_lines, list):
        raise ValueError("alignment JSON missing 'lines' list")

    aligned_lines: list[AlignedLine] = []
    for i, line in enumerate(raw_lines):
        if not isinstance(line, dict):
            raise ValueError(f"lines[{i}] must be an object")
        text = line.get("text")
        words_raw = line.get("words")
        if not isinstance(text, str) or not isinstance(words_raw, list):
            raise ValueError(f"lines[{i}] needs string 'text' and list 'words'")
        words: list[AlignedWord] = []
        for j, w in enumerate(words_raw):
            if not isinstance(w, dict):
                raise ValueError(f"lines[{i}].words[{j}] must be an object")
            wt = w.get("text")
            ws = w.get("start")
            we = w.get("end")
            if not isinstance(wt, str) or not isinstance(ws, (int, float)):
                raise ValueError(
                    f"lines[{i}].words[{j}] needs text and numeric start"
                )
            if not isinstance(we, (int, float)):
                raise ValueError(
                    f"lines[{i}].words[{j}] needs numeric end"
                )
            s, e = float(ws), float(we)
            if s < 0 or e < 0:
                s, e = max(0.0, s), max(0.0, e)
            if e < s:
                e = s
            words.append(AlignedWord(text=wt, start=s, end=e))
        if not words:
            continue
        chunk = tuple(words)
        aligned_lines.append(
            AlignedLine(
                text=text,
                start=chunk[0].start,
                end=chunk[-1].end,
                words=chunk,
            )
        )

    return AlignmentResult(lines=tuple(aligned_lines))


def write_alignment_json(
    result: AlignmentResult,
    path: Path,
    meta: dict[str, Any] | None = None,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = alignment_to_dict(result, meta=meta)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_alignment_json(path: Path) -> AlignmentResult:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("alignment JSON root must be an object")
    return alignment_from_dict(data)
