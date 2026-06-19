"""Timeline (EDL) model for story rendering.

Schema, validation, edit operations, and the timeline → ffmpeg filtergraph
compiler. Pure stdlib — no Modal/FastAPI/network deps — so it imports cleanly
in tests (mirrors transcript.py).

A timeline replaces the flat ranges_json quote-list as the editable
representation of a story. Quote resolution still produces the *initial*
timeline (timeline_from_ranges); after that, edits go through apply_ops and
the render worker compiles whatever the timeline says.

Schema (version 1):

    {
      "version": 1,
      "width": 1080, "height": 1920, "fps": 30,
      "tracks": [
        {"type": "video", "items": [
          {"id": "v1", "kind": "clip", "source": "IMG_2415.mov",
           "src_start": 12.40, "src_end": 18.20, "speed": 1.0,
           "transition_in": null, "note": "optional transcript text"},
          {"id": "v2", "kind": "blank", "duration": 1.0,
           "transition_in": {"type": "crossfade", "duration": 0.3}}
        ]},
        {"type": "text", "items": [
          {"id": "t1", "text": "Title card", "start": 0.0, "end": 3.0,
           "size": 64, "position": "center", "wrap": 22}
        ]}
      ]
    }

Conventions:
  - Video items play in array order. src_start/src_end are source-clip seconds;
    speed changes playback rate (output duration = span / speed).
  - transition_in describes how an item is joined to the PREVIOUS item
    (null = hard cut). It is meaningless on the first item.
  - Text items live in OUTPUT time (seconds into the rendered video), so they
    survive reordering of the video track underneath them.
"""

from __future__ import annotations

import copy
import re
import textwrap

TIMELINE_VERSION = 1

DEFAULT_W = 1080
DEFAULT_H = 1920
DEFAULT_FPS = 30
AUDIO_SR = 48000

# Padding baked into timelines built from quote ranges, matching the implicit
# ±0.08 s the legacy render applied around each trim point.
RANGE_PAD_S = 0.08

MIN_SPEED, MAX_SPEED = 0.25, 20.0  # up to 20x for time-lapse speed-ups
MAX_CROSSFADE_S = 3.0
TEXT_POSITIONS = ("center", "upper", "lower")

# Optional per-clip audio effects, applied after the speed/resample stage.
# Each value is an ffmpeg audio-filter chain (aecho gives an echo/reverb wash) —
# handy for exaggerated "bad room" sounds in before/after gags. aecho preserves
# clip length, so timeline timing is unaffected. Pick via a clip item's
# `audio_fx` field (e.g. {"kind":"clip", ..., "audio_fx":"cavern"}).
AUDIO_FX = {
    "echo": "aecho=0.8:0.88:120:0.5",
    "reverb": "aecho=0.8:0.9:60|110|190|300:0.5|0.4|0.3|0.2",
    "cavern": (
        "aecho=0.9:0.92:90|180|320|520:0.65|0.5|0.4|0.3,"
        "aecho=0.7:0.85:700:0.35"
    ),
}

_ID_RE = re.compile(r"^([a-z]+)(\d+)$")


def choose_canvas(
    dims: list[tuple[int, int]],
    default_w: int = DEFAULT_W,
    default_h: int = DEFAULT_H,
    tol: float = 0.02,
) -> tuple[int, int]:
    """Pick an output canvas (w, h) for a set of source-clip display sizes.

    `dims` are the rotation-corrected (display) width/height of each non-blank
    source clip in a timeline. The aim is to avoid letterboxing: when every
    clip shares an aspect ratio we size the canvas to match it so the render's
    scale+pad normalize step pads nothing.

    Rules:
      - No dims (e.g. a blank-only timeline) -> keep the default frame.
      - Mixed aspect ratios (spread > tol) -> keep the default; one canvas
        can't fit them all without padding something, so fall back rather than
        guess and crop.
      - Uniform aspect -> short side = 1080, long side scaled to match and
        capped so it never exceeds 1920. Both dims are rounded to even numbers
        (h.264 requires even width/height).
    """
    usable = [(w, h) for (w, h) in dims if w > 0 and h > 0]
    if not usable:
        return (default_w, default_h)

    ars = [w / h for (w, h) in usable]
    if max(ars) - min(ars) > tol:
        return (default_w, default_h)
    ar = sum(ars) / len(ars)

    short, long_cap = 1080, 1920
    if ar <= 1.0:  # portrait or square -> width is the short side
        w = short
        h = round(w / ar)
        if h > long_cap:
            h, w = long_cap, round(long_cap * ar)
    else:  # landscape -> height is the short side
        h = short
        w = round(h * ar)
        if w > long_cap:
            w, h = long_cap, round(long_cap / ar)

    return (w - w % 2, h - h % 2)


class TimelineError(ValueError):
    """Raised for invalid timelines or edit operations. The message is meant
    to be returned verbatim to the API caller (an LLM), so be specific."""


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def video_items(timeline: dict) -> list[dict]:
    for track in timeline.get("tracks", []):
        if track.get("type") == "video":
            return track.setdefault("items", [])
    raise TimelineError("timeline has no video track")


def text_items(timeline: dict, create: bool = False) -> list[dict]:
    for track in timeline.get("tracks", []):
        if track.get("type") == "text":
            return track.setdefault("items", [])
    if create:
        track = {"type": "text", "items": []}
        timeline.setdefault("tracks", []).append(track)
        return track["items"]
    return []


def _all_ids(timeline: dict) -> set[str]:
    ids: set[str] = set()
    for track in timeline.get("tracks", []):
        for item in track.get("items", []):
            if item.get("id"):
                ids.add(item["id"])
    return ids


def _new_id(timeline: dict, prefix: str) -> str:
    """Next free sequential id like v7 / t3 — stable and readable in diffs."""
    used = 0
    for existing in _all_ids(timeline):
        m = _ID_RE.match(existing)
        if m and m.group(1) == prefix:
            used = max(used, int(m.group(2)))
    return f"{prefix}{used + 1}"


def item_duration(item: dict) -> float:
    """Output duration of a video item in seconds (after speed)."""
    if item.get("kind") == "blank":
        return float(item["duration"])
    span = float(item["src_end"]) - float(item["src_start"])
    return span / float(item.get("speed") or 1.0)


def timeline_duration(timeline: dict) -> float:
    """Total output duration: item durations minus crossfade overlaps."""
    total = 0.0
    for i, item in enumerate(video_items(timeline)):
        total += item_duration(item)
        tr = item.get("transition_in")
        if i > 0 and tr and tr.get("type") == "crossfade":
            total -= float(tr["duration"])
    return total


# ---------------------------------------------------------------------------
# Legacy import: ranges_json → timeline
# ---------------------------------------------------------------------------

def timeline_from_ranges(ranges: list[dict]) -> dict:
    """Build a v1 timeline from legacy ranges_json.

    The legacy render padded every trim point by ±0.08 s at render time; here
    the pad is baked into src_start/src_end instead, so the timeline is
    WYSIWYG — what the items say is exactly what renders.

    Legacy per-range overlays (segment-local times) become text items in
    output time.
    """
    timeline: dict = {
        "version": TIMELINE_VERSION,
        "width": DEFAULT_W,
        "height": DEFAULT_H,
        "fps": DEFAULT_FPS,
        "tracks": [{"type": "video", "items": []}],
    }
    vitems = video_items(timeline)
    texts: list[dict] = []
    out_pos = 0.0  # cumulative output time, for overlay conversion

    for i, rng in enumerate(ranges or []):
        if rng.get("source") == "blank":
            dur = float(rng["end"]) - float(rng["start"])
            item = {
                "id": f"v{i + 1}",
                "kind": "blank",
                "duration": round(dur, 3),
                "transition_in": None,
            }
        else:
            src_start = max(0.0, float(rng["start"]) - RANGE_PAD_S)
            src_end = float(rng["end"]) + RANGE_PAD_S
            item = {
                "id": f"v{i + 1}",
                "kind": "clip",
                "source": rng["source"],
                "src_start": round(src_start, 3),
                "src_end": round(src_end, 3),
                "speed": 1.0,
                "transition_in": None,
            }
            if rng.get("text"):
                item["note"] = rng["text"]
        vitems.append(item)

        seg_len = item_duration(item)
        overlay = rng.get("overlay")
        if overlay and overlay.get("text"):
            t_in = float(overlay.get("in") or 0.0)
            t_out = (
                float(overlay["out"])
                if overlay.get("out") is not None
                else seg_len
            )
            text: dict = {
                "id": f"t{len(texts) + 1}",
                "text": str(overlay["text"]),
                "start": round(out_pos + t_in, 3),
                "end": round(out_pos + min(t_out, seg_len), 3),
            }
            for key in ("size", "position", "wrap"):
                if overlay.get(key) is not None:
                    text[key] = overlay[key]
            texts.append(text)
        out_pos += seg_len

    if texts:
        timeline["tracks"].append({"type": "text", "items": texts})
    return timeline


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_timeline(timeline: dict) -> list[str]:
    """Return a list of error strings; empty list means the timeline renders."""
    errors: list[str] = []
    if not isinstance(timeline, dict):
        return ["timeline must be an object"]
    if timeline.get("version") != TIMELINE_VERSION:
        errors.append(
            f"unsupported timeline version {timeline.get('version')!r} "
            f"(expected {TIMELINE_VERSION})"
        )
        return errors

    tracks = timeline.get("tracks")
    if not isinstance(tracks, list):
        return errors + ["tracks must be an array"]
    vtracks = [t for t in tracks if t.get("type") == "video"]
    ttracks = [t for t in tracks if t.get("type") == "text"]
    if len(vtracks) != 1:
        errors.append(f"timeline must have exactly 1 video track (found {len(vtracks)})")
    if len(ttracks) > 1:
        errors.append(f"timeline must have at most 1 text track (found {len(ttracks)})")
    if len(vtracks) + len(ttracks) != len(tracks):
        errors.append("tracks may only be of type 'video' or 'text'")
    if errors:
        return errors

    seen_ids: set[str] = set()

    vitems = vtracks[0].get("items") or []
    if not vitems:
        errors.append("video track has no items — nothing to render")

    durations: list[float] = []
    for i, item in enumerate(vitems):
        label = f"video item {item.get('id') or f'#{i}'}"
        iid = item.get("id")
        if not iid or not isinstance(iid, str):
            errors.append(f"{label}: missing id")
        elif iid in seen_ids:
            errors.append(f"{label}: duplicate id")
        else:
            seen_ids.add(iid)

        kind = item.get("kind")
        if kind == "clip":
            if not item.get("source") or not isinstance(item["source"], str):
                errors.append(f"{label}: clip items need a source filename")
            try:
                s, e = float(item["src_start"]), float(item["src_end"])
                if s < 0:
                    errors.append(f"{label}: src_start must be >= 0")
                if e <= s:
                    errors.append(f"{label}: src_end must be > src_start")
            except (KeyError, TypeError, ValueError):
                errors.append(f"{label}: src_start/src_end must be numbers")
                durations.append(0.0)
                continue
            speed = item.get("speed", 1.0)
            if not isinstance(speed, (int, float)) or not (
                MIN_SPEED <= float(speed) <= MAX_SPEED
            ):
                errors.append(
                    f"{label}: speed must be between {MIN_SPEED} and {MAX_SPEED}"
                )
                durations.append(max(0.0, e - s))
                continue
            fx = item.get("audio_fx")
            if fx is not None and fx not in AUDIO_FX:
                errors.append(
                    f"{label}: audio_fx must be one of {sorted(AUDIO_FX)} "
                    f"(got {fx!r})"
                )
                durations.append(max(0.0, e - s))
                continue
            mute = item.get("mute")
            if mute is not None and not isinstance(mute, bool):
                errors.append(f"{label}: mute must be true or false (got {mute!r})")
                durations.append(max(0.0, e - s))
                continue
        elif kind == "blank":
            dur = item.get("duration")
            if not isinstance(dur, (int, float)) or float(dur) <= 0:
                errors.append(f"{label}: blank items need duration > 0")
                durations.append(0.0)
                continue
        else:
            errors.append(f"{label}: kind must be 'clip' or 'blank'")
            durations.append(0.0)
            continue
        durations.append(item_duration(item))

    for i, item in enumerate(vitems):
        label = f"video item {item.get('id') or f'#{i}'}"
        tr = item.get("transition_in")
        if tr is None:
            continue
        if i == 0:
            errors.append(f"{label}: first item cannot have a transition_in")
            continue
        if not isinstance(tr, dict) or tr.get("type") != "crossfade":
            errors.append(f"{label}: transition_in must be null or "
                          "{{'type': 'crossfade', 'duration': s}}")
            continue
        td = tr.get("duration")
        if not isinstance(td, (int, float)) or not (0 < float(td) <= MAX_CROSSFADE_S):
            errors.append(
                f"{label}: crossfade duration must be in (0, {MAX_CROSSFADE_S}]"
            )
            continue
        if i < len(durations) and float(td) >= min(durations[i - 1], durations[i]):
            errors.append(
                f"{label}: crossfade ({float(td):.2f}s) must be shorter than "
                f"both adjacent items "
                f"({durations[i - 1]:.2f}s and {durations[i]:.2f}s)"
            )

    titems = (ttracks[0].get("items") or []) if ttracks else []
    for i, item in enumerate(titems):
        label = f"text item {item.get('id') or f'#{i}'}"
        iid = item.get("id")
        if not iid or not isinstance(iid, str):
            errors.append(f"{label}: missing id")
        elif iid in seen_ids:
            errors.append(f"{label}: duplicate id")
        else:
            seen_ids.add(iid)
        if not str(item.get("text") or "").strip():
            errors.append(f"{label}: text must be non-empty")
        try:
            s, e = float(item["start"]), float(item["end"])
            if s < 0:
                errors.append(f"{label}: start must be >= 0")
            if e <= s:
                errors.append(f"{label}: end must be > start")
        except (KeyError, TypeError, ValueError):
            errors.append(f"{label}: start/end must be numbers")
        size = item.get("size")
        if size is not None and not (
            isinstance(size, (int, float)) and 12 <= size <= 200
        ):
            errors.append(f"{label}: size must be between 12 and 200")
        pos = item.get("position")
        if pos is not None and pos not in TEXT_POSITIONS:
            errors.append(
                f"{label}: position must be one of {', '.join(TEXT_POSITIONS)}"
            )
        wrap = item.get("wrap")
        if wrap is not None and not (
            isinstance(wrap, (int, float)) and 4 <= wrap <= 80
        ):
            errors.append(f"{label}: wrap must be between 4 and 80")

    return errors


# ---------------------------------------------------------------------------
# Edit operations
# ---------------------------------------------------------------------------

def _find_video(timeline: dict, item_id: str) -> tuple[int, dict]:
    for i, item in enumerate(video_items(timeline)):
        if item.get("id") == item_id:
            return i, item
    raise TimelineError(f"no video item with id {item_id!r}")


def _find_text(timeline: dict, item_id: str) -> dict:
    for item in text_items(timeline):
        if item.get("id") == item_id:
            return item
    raise TimelineError(f"no text item with id {item_id!r}")


def _require_clip(item: dict, op: str) -> None:
    if item.get("kind") != "clip":
        raise TimelineError(
            f"{op} only applies to clip items, {item.get('id')!r} is "
            f"{item.get('kind')!r}"
        )


def _op_trim(timeline: dict, op: dict) -> None:
    _, item = _find_video(timeline, op.get("id", ""))
    _require_clip(item, "trim")
    src_start = float(item["src_start"])
    src_end = float(item["src_end"])
    if op.get("src_start") is not None:
        src_start = float(op["src_start"])
    if op.get("src_end") is not None:
        src_end = float(op["src_end"])
    if op.get("start_delta") is not None:
        src_start += float(op["start_delta"])
    if op.get("end_delta") is not None:
        src_end += float(op["end_delta"])
    if src_start < 0:
        raise TimelineError(
            f"trim on {item['id']}: src_start would be {src_start:.3f} (< 0)"
        )
    if src_end <= src_start:
        raise TimelineError(
            f"trim on {item['id']}: span would be empty "
            f"({src_start:.3f}–{src_end:.3f})"
        )
    item["src_start"] = round(src_start, 3)
    item["src_end"] = round(src_end, 3)


def _op_split(timeline: dict, op: dict) -> None:
    idx, item = _find_video(timeline, op.get("id", ""))
    _require_clip(item, "split")
    span = float(item["src_end"]) - float(item["src_start"])
    at = float(op.get("at", -1))
    if not (0 < at < span):
        raise TimelineError(
            f"split on {item['id']}: 'at' is seconds into the item's source "
            f"span and must be between 0 and {span:.3f} (got {at})"
        )
    second = copy.deepcopy(item)
    second["id"] = _new_id(timeline, "v")
    second["src_start"] = round(float(item["src_start"]) + at, 3)
    second["transition_in"] = None
    item["src_end"] = round(float(item["src_start"]) + at, 3)
    video_items(timeline).insert(idx + 1, second)


def _op_move(timeline: dict, op: dict) -> None:
    idx, _ = _find_video(timeline, op.get("id", ""))
    items = video_items(timeline)
    to = op.get("to_index")
    if not isinstance(to, int) or not (0 <= to < len(items)):
        raise TimelineError(
            f"move: to_index must be between 0 and {len(items) - 1} (got {to!r})"
        )
    items.insert(to, items.pop(idx))


def _op_delete(timeline: dict, op: dict) -> None:
    idx, _ = _find_video(timeline, op.get("id", ""))
    video_items(timeline).pop(idx)


def _op_set_speed(timeline: dict, op: dict) -> None:
    _, item = _find_video(timeline, op.get("id", ""))
    _require_clip(item, "set_speed")
    speed = op.get("speed")
    if not isinstance(speed, (int, float)) or not (
        MIN_SPEED <= float(speed) <= MAX_SPEED
    ):
        raise TimelineError(
            f"set_speed: speed must be between {MIN_SPEED} and {MAX_SPEED} "
            f"(got {speed!r})"
        )
    item["speed"] = float(speed)


def _op_set_mute(timeline: dict, op: dict) -> None:
    _, item = _find_video(timeline, op.get("id", ""))
    _require_clip(item, "set_mute")
    mute = op.get("mute")
    if not isinstance(mute, bool):
        raise TimelineError(
            f"set_mute: mute must be true or false (got {mute!r})"
        )
    if mute:
        item["mute"] = True
    else:
        item.pop("mute", None)


def _op_set_transition(timeline: dict, op: dict) -> None:
    idx, item = _find_video(timeline, op.get("id", ""))
    tr = op.get("transition")
    if tr is not None:
        if idx == 0:
            raise TimelineError(
                "set_transition: the first item cannot have a transition_in"
            )
        if not isinstance(tr, dict) or tr.get("type") != "crossfade":
            raise TimelineError(
                "set_transition: transition must be null or "
                "{'type': 'crossfade', 'duration': seconds}"
            )
    item["transition_in"] = tr


def _op_insert_clip(timeline: dict, op: dict) -> None:
    items = video_items(timeline)
    item = {
        "id": _new_id(timeline, "v"),
        "kind": "clip",
        "source": op.get("source"),
        "src_start": op.get("src_start"),
        "src_end": op.get("src_end"),
        "speed": float(op.get("speed") or 1.0),
        "transition_in": None,
    }
    index = op.get("index")
    if index is None:
        index = len(items)
    if not isinstance(index, int) or not (0 <= index <= len(items)):
        raise TimelineError(
            f"insert_clip: index must be between 0 and {len(items)} (got {index!r})"
        )
    items.insert(index, item)


def _op_insert_blank(timeline: dict, op: dict) -> None:
    items = video_items(timeline)
    item = {
        "id": _new_id(timeline, "v"),
        "kind": "blank",
        "duration": op.get("duration"),
        "transition_in": None,
    }
    index = op.get("index")
    if index is None:
        index = len(items)
    if not isinstance(index, int) or not (0 <= index <= len(items)):
        raise TimelineError(
            f"insert_blank: index must be between 0 and {len(items)} (got {index!r})"
        )
    items.insert(index, item)


def _op_add_text(timeline: dict, op: dict) -> None:
    items = text_items(timeline, create=True)
    item = {
        "id": _new_id(timeline, "t"),
        "text": op.get("text"),
        "start": op.get("start"),
        "end": op.get("end"),
    }
    for key in ("size", "position", "wrap"):
        if op.get(key) is not None:
            item[key] = op[key]
    items.append(item)


def _op_update_text(timeline: dict, op: dict) -> None:
    item = _find_text(timeline, op.get("id", ""))
    for key in ("text", "start", "end", "size", "position", "wrap"):
        if op.get(key) is not None:
            item[key] = op[key]


def _op_remove_text(timeline: dict, op: dict) -> None:
    items = text_items(timeline)
    for i, item in enumerate(items):
        if item.get("id") == op.get("id"):
            items.pop(i)
            return
    raise TimelineError(f"no text item with id {op.get('id')!r}")


_OPS = {
    "trim": _op_trim,
    "split": _op_split,
    "move": _op_move,
    "delete": _op_delete,
    "set_speed": _op_set_speed,
    "set_mute": _op_set_mute,
    "set_transition": _op_set_transition,
    "insert_clip": _op_insert_clip,
    "insert_blank": _op_insert_blank,
    "add_text": _op_add_text,
    "update_text": _op_update_text,
    "remove_text": _op_remove_text,
}


def apply_ops(timeline: dict, ops: list[dict]) -> dict:
    """Apply edit operations to a copy of the timeline and validate the result.

    Raises TimelineError with an LLM-readable message naming the failing op.
    The input timeline is never mutated.
    """
    result = copy.deepcopy(timeline)
    for i, op in enumerate(ops or []):
        name = (op or {}).get("op")
        fn = _OPS.get(name)
        if fn is None:
            raise TimelineError(
                f"op {i}: unknown op {name!r} (valid: {', '.join(sorted(_OPS))})"
            )
        try:
            fn(result, op)
        except TimelineError as exc:
            raise TimelineError(f"op {i} ({name}): {exc}") from exc
        # Structural ops can promote an item to first position; a leading
        # transition_in has nothing to fade from, so drop it.
        vitems = video_items(result)
        if vitems and vitems[0].get("transition_in"):
            vitems[0]["transition_in"] = None

    errors = validate_timeline(result)
    if errors:
        raise TimelineError("resulting timeline is invalid: " + "; ".join(errors))
    return result


# ---------------------------------------------------------------------------
# Compiler: timeline → ffmpeg inputs + filter_complex
# ---------------------------------------------------------------------------

def _atempo_chain(speed: float) -> str:
    """Decompose a speed factor into chained atempo filters (each 0.5–2.0)."""
    factors: list[float] = []
    s = float(speed)
    while s > 2.0:
        factors.append(2.0)
        s /= 2.0
    while s < 0.5:
        factors.append(0.5)
        s /= 0.5
    factors.append(s)
    return ",".join(f"atempo={f:.6g}" for f in factors)


def _drawtext(item: dict, textfile: str, font_path: str) -> str:
    """Build one drawtext filter for a text item (output-time enable window)."""
    size = int(item.get("size") or 64)
    pos = str(item.get("position") or "center").lower()
    yexpr = {
        "upper": "h*0.10",
        "lower": "h*0.72",
    }.get(pos, "(h-text_h)/2")
    start = float(item["start"])
    end = float(item["end"])
    # Commas inside the enable expression must be escaped within filter_complex.
    enable = f"between(t\\,{start:.3f}\\,{end:.3f})"
    return (
        f"drawtext=fontfile={font_path}:textfile={textfile}:"
        f"fontcolor=white:fontsize={size}:line_spacing=14:"
        f"box=1:boxcolor=black@0.5:boxborderw=30:"
        f"x=(w-text_w)/2:y={yexpr}:enable='{enable}'"
    )


def compile_timeline(
    timeline: dict,
    resolve_source,
    workdir: str,
    font_path: str,
) -> dict:
    """Compile a timeline into ffmpeg invocation pieces.

    resolve_source(filename) -> local file path for clip items.

    Returns {
      "inputs":         list of ffmpeg input-arg lists, in filter-index order,
      "filter_complex": the full filtergraph string (maps [vout] / [aout]),
      "text_files":     [(path, content)] the caller must write before running,
      "duration":       expected output duration in seconds,
    }
    """
    errors = validate_timeline(timeline)
    if errors:
        raise TimelineError("cannot compile invalid timeline: " + "; ".join(errors))

    w = int(timeline.get("width") or DEFAULT_W)
    h = int(timeline.get("height") or DEFAULT_H)
    fps = int(timeline.get("fps") or DEFAULT_FPS)
    sr = AUDIO_SR

    vitems = video_items(timeline)
    titems = text_items(timeline)
    has_xfade = any(
        (it.get("transition_in") or {}).get("type") == "crossfade"
        for it in vitems
    )

    vnorm = (
        f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
        f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2:color=black,"
        f"setsar=1,fps={fps}"
    )
    # xfade needs identical timebases on both inputs; harmless otherwise but
    # only added when crossfades are present to keep the cut-only graph
    # byte-identical with the long-proven legacy one.
    if has_xfade:
        vnorm += ",settb=AVTB"

    inputs: list[list[str]] = []
    parts: list[str] = []
    in_idx = 0

    for i, item in enumerate(vitems):
        if item.get("kind") == "blank":
            dur = float(item["duration"])
            inputs.append([
                "-f", "lavfi", "-i",
                f"color=c=black:s={w}x{h}:d={dur:.3f}:r={fps}",
            ])
            inputs.append([
                "-f", "lavfi", "-i",
                f"anullsrc=channel_layout=stereo:sample_rate={sr}",
            ])
            vidx, aidx = in_idx, in_idx + 1
            in_idx += 2
            parts.append(f"[{vidx}:v]setpts=PTS-STARTPTS,{vnorm}[v{i}]")
            parts.append(
                f"[{aidx}:a]asetpts=PTS-STARTPTS,"
                f"aresample={sr},aformat=channel_layouts=stereo,"
                f"atrim=duration={dur:.3f}[a{i}]"
            )
            continue

        path = resolve_source(item["source"])
        src_start = float(item["src_start"])
        span = float(item["src_end"]) - src_start
        speed = float(item.get("speed") or 1.0)
        inputs.append(["-ss", f"{src_start:.3f}", "-t", f"{span:.3f}", "-i", str(path)])
        vidx = in_idx
        in_idx += 1

        if speed != 1.0:
            vsetpts = f"setpts=(PTS-STARTPTS)/{speed:.6g}"
            aspeed = _atempo_chain(speed) + ","
        else:
            vsetpts = "setpts=PTS-STARTPTS"
            aspeed = ""
        afx = item.get("audio_fx")
        afx_chain = "," + AUDIO_FX[afx] if afx in AUDIO_FX else ""
        # `mute` silences the clip's audio (kept length-matched via volume=0) —
        # e.g. a silent time-lapse. Applied last so it overrides any audio_fx.
        mute_chain = ",volume=0" if item.get("mute") else ""
        parts.append(f"[{vidx}:v]{vsetpts},{vnorm}[v{i}]")
        parts.append(
            f"[{vidx}:a]asetpts=PTS-STARTPTS,{aspeed}"
            f"aresample={sr},aformat=channel_layouts=stereo{afx_chain}{mute_chain}[a{i}]"
        )

    # Join the per-item streams.
    if len(vitems) == 1:
        cv, ca = "v0", "a0"
        out_dur = item_duration(vitems[0])
    elif not has_xfade:
        concat_in = "".join(f"[v{i}][a{i}]" for i in range(len(vitems)))
        parts.append(f"{concat_in}concat=n={len(vitems)}:v=1:a=1[vcat][acat]")
        cv, ca = "vcat", "acat"
        out_dur = sum(item_duration(it) for it in vitems)
    else:
        cv, ca = "v0", "a0"
        out_dur = item_duration(vitems[0])
        for k, item in enumerate(vitems[1:], start=1):
            d = item_duration(item)
            i = k  # per-item stream labels [v{i}]/[a{i}] use the vitems index
            tr = item.get("transition_in")
            if tr and tr.get("type") == "crossfade":
                td = float(tr["duration"])
                parts.append(
                    f"[{cv}][v{i}]xfade=transition=fade:"
                    f"duration={td:.3f}:offset={out_dur - td:.3f}[xv{k}]"
                )
                parts.append(f"[{ca}][a{i}]acrossfade=d={td:.3f}[xa{k}]")
                out_dur += d - td
            else:
                parts.append(
                    f"[{cv}][{ca}][v{i}][a{i}]concat=n=2:v=1:a=1[xv{k}][xa{k}]"
                )
                out_dur += d
            cv, ca = f"xv{k}", f"xa{k}"

    # Text track: drawtext on the joined stream in output time, so text
    # placement is independent of how the video track is sliced underneath.
    text_files: list[tuple[str, str]] = []
    draw_filters: list[str] = []
    for item in titems:
        wrap = int(item.get("wrap") or 22)
        raw = str(item["text"]).strip()
        wrapped = "\n".join(textwrap.wrap(raw, width=wrap)) or raw
        # textfile= sidesteps escaping colons/quotes/commas in the copy itself.
        path = f"{workdir}/text_{item['id']}.txt"
        text_files.append((path, wrapped))
        draw_filters.append(_drawtext(item, path, font_path))

    draw = (",".join(draw_filters) + ",") if draw_filters else ""
    parts.append(f"[{cv}]{draw}format=yuv420p[vout]")
    parts.append(f"[{ca}]anull[aout]")

    return {
        "inputs": inputs,
        "filter_complex": ";".join(parts),
        "text_files": text_files,
        "duration": round(out_dur, 3),
    }
