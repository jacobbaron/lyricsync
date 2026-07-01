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
           "clip_id": null,  # optional: global clip uuid for cross-project cuts
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
import math
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
            # A clip item is resolvable either by a bare `source` filename
            # (within the story's home project) or by a global `clip_id`
            # (cross-project; see docs/cross_project_editing.md). When `clip_id`
            # is present it is the authoritative reference and `source` is just
            # a human-readable label, so a source filename is optional then.
            has_clip_id = bool(
                isinstance(item.get("clip_id"), str) and item.get("clip_id")
            )
            if not has_clip_id and (
                not item.get("source") or not isinstance(item["source"], str)
            ):
                errors.append(
                    f"{label}: clip items need a source filename or a clip_id"
                )
            if item.get("clip_id") is not None and not has_clip_id:
                errors.append(f"{label}: clip_id must be a non-empty string")
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

    errors.extend(_validate_music(timeline.get("music")))
    return errors


def _validate_music(music: object) -> list[str]:
    """Validate the optional top-level `music` object (external track under the
    footage). Returns error strings; empty when absent or well-formed.

    Shape: {song_id: str, song_start: >=0, gain_db?: num, scratch_gain_db?: num|None}.
    `scratch_gain_db=None` (or absent) means replace the clip audio with the song;
    a number means duck the clip audio to that gain (dB) and mix the song over it.
    """
    if music is None:
        return []
    if not isinstance(music, dict):
        return ["music must be an object"]
    errors: list[str] = []
    if not isinstance(music.get("song_id"), str) or not music.get("song_id"):
        errors.append("music: song_id must be a non-empty string")
    try:
        if float(music.get("song_start")) < 0:
            errors.append("music: song_start must be >= 0")
    except (TypeError, ValueError):
        errors.append("music: song_start must be a number")
    for key in ("gain_db", "scratch_gain_db"):
        v = music.get(key)
        if v is not None and not isinstance(v, (int, float)):
            errors.append(f"music: {key} must be a number or null")
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
    # Optional global clip reference (cross-project; see
    # docs/cross_project_editing.md). When present it is the authoritative
    # source; `source` may still be supplied as a human-readable label.
    if op.get("clip_id") is not None:
        item["clip_id"] = op.get("clip_id")
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


def _audio_for_item(
    item: dict,
    audio_by_source: dict | None,
    audio_by_clip_id: dict | None,
) -> dict | None:
    """Resolve one clip item's stored audio analysis.

    Cross-project clips (with a global `clip_id`) read from
    `audio_by_clip_id[clip_id]` — keyed globally, so a foreign clip's analysis
    is found regardless of the home project. Local clips fall back to the legacy
    filename-keyed `audio_by_source`. See docs/cross_project_editing.md (Tier 2).
    """
    cid = item.get("clip_id")
    if cid and audio_by_clip_id is not None and cid in audio_by_clip_id:
        return audio_by_clip_id.get(cid)
    return (audio_by_source or {}).get(item.get("source"))


def apply_ops(
    timeline: dict, ops: list[dict], words: list[dict] | None = None,
    audio_by_source: dict | None = None,
    words_by_clip_id: dict | None = None,
    audio_by_clip_id: dict | None = None,
) -> dict:
    """Apply edit operations to a copy of the timeline and validate the result.

    Raises TimelineError with an LLM-readable message naming the failing op.
    The input timeline is never mutated.

    `words` are the home project's aligned words; required only when an op needs
    transcript timing (currently just `clean_speech`). Passing them is cheap
    and harmless when no such op is present.

    `audio_by_source` maps a clip filename to its stored audio analysis
    ({vad_prob, vad_hop, peaks, peaks_hop}); when present for a clean_speech
    target, the VAD-fused silence crop is used instead of word-gap timing.

    `words_by_clip_id` / `audio_by_clip_id` (Tier 2, cross-project) are keyed by
    a clip's global `clip_id`. When a clean_speech target carries a `clip_id`,
    its words/audio are taken from these maps (already scoped to that clip's
    owning project) instead of the home-project filename lookup — so editing a
    foreign clip works, and a filename shared by two clips in different projects
    never cross-contaminates. Local clips (no `clip_id`) keep the legacy path.
    """
    result = copy.deepcopy(timeline)
    valid = sorted(list(_OPS) + ["clean_speech"])
    for i, op in enumerate(ops or []):
        name = (op or {}).get("op")
        if name == "clean_speech":
            if words is None and words_by_clip_id is None:
                raise TimelineError(
                    f"op {i} (clean_speech): no transcript is loaded for this "
                    "story, so word timings are unavailable"
                )
            try:
                item_id = (op or {}).get("id", "")
                target = None
                for it in video_items(result):
                    if it.get("id") == item_id:
                        target = it
                        break
                # Tier 2 (shipped): a clean_speech target carrying a global
                # clip_id resolves its words/audio per-clip from the *_by_clip_id
                # maps (the clip's owning project), so cross-project cleanup
                # works exactly like local. See docs/cross_project_editing.md.
                audio = _audio_for_item(
                    target or {}, audio_by_source, audio_by_clip_id
                )
                result, _ = expand_clean_speech(
                    result, item_id, words, (op or {}).get("params"), audio,
                    words_by_clip_id=words_by_clip_id,
                )
            except TimelineError as exc:
                raise TimelineError(f"op {i} ({name}): {exc}") from exc
            vitems = video_items(result)
            if vitems and vitems[0].get("transition_in"):
                vitems[0]["transition_in"] = None
            continue
        fn = _OPS.get(name)
        if fn is None:
            raise TimelineError(
                f"op {i}: unknown op {name!r} (valid: {', '.join(valid)})"
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
# Speech cleanup planning
# ---------------------------------------------------------------------------
#
# Given the aligned words that fall inside one clip's source span, decide which
# sub-spans to KEEP so the span plays tight: collapse over-long pauses, drop
# filler words, and trim dead air at the head/tail — while leaving genuine
# non-speech untouched. This is pure analysis over word timings; turning the
# resulting keep-list into timeline items (jump cuts) is a separate edit op.
#
# Vocabulary note: a "gap" here is silence *between two kept words*. We never
# invent speech boundaries — every keep edge is anchored to a real word's
# aligned start/end (optionally padded), so the picture stays WYSIWYG.

# Conservative default filler set: non-lexical disfluencies only. Words like
# "like" / "so" / "you know" are deliberately excluded because they are usually
# content; callers opt into those explicitly via `filler_lexicon`.
DEFAULT_FILLERS: tuple[str, ...] = (
    "um", "umm", "uh", "uhh", "uhm", "er", "err", "erm",
    "ah", "ahh", "eh", "hmm", "hm", "mm", "mhm", "uh-huh",
)

SPEECH_CLEANUP_DEFAULTS: dict = {
    # Silence between kept words longer than this (seconds) gets collapsed…
    "max_gap": 0.35,
    # …down to this much retained "breath" (never to 0, which sounds clipped).
    "collapse_to": 0.15,
    # Pauses longer than this are treated as intentional and left intact, so a
    # deliberate beat survives. None disables the guard (collapse everything).
    "protect_gap_over": 2.0,
    # Drop filler words entirely.
    "remove_fillers": True,
    "filler_lexicon": DEFAULT_FILLERS,
    # Padding (seconds) added around every kept word so tight cuts don't clip
    # consonants — forced alignment tends to truncate word edges slightly.
    "pad_start": 0.04,
    "pad_end": 0.06,
    # Confidence handling: words whose alignment score is below `min_score`
    # get `low_score_pad` extra seconds of padding on each side (be cautious
    # where the boundary is uncertain). None => ignore scores entirely.
    "min_score": None,
    "low_score_pad": 0.06,
    # Tighten leading/trailing silence inside the span down to the word pad.
    "trim_lead": True,
    "trim_tail": True,
    # Don't bother emitting a cut that would reclaim less than this (avoids
    # pointless micro jump-cuts that only add visual jitter).
    "min_removed": 0.08,
}


def _norm_word(text: str) -> str:
    """Lowercase, keep only alnum + apostrophe + hyphen — for filler matching."""
    return "".join(
        ch for ch in (text or "").lower() if ch.isalnum() or ch in "'-"
    )


def _word_bounds(w: dict) -> tuple[float, float]:
    """Read a word's (start, end) accepting either local_* or plain keys."""
    start = w.get("start", w.get("local_start"))
    end = w.get("end", w.get("local_end"))
    return float(start), float(end)


def _filler_flags(words: list[dict], lexicon) -> list[bool]:
    """Mark which words are fillers, supporting multi-word phrases (e.g.
    "you know"). Longest phrases match first; matched words can't re-match."""
    phrases = set()
    for entry in lexicon or ():
        toks = tuple(_norm_word(t) for t in str(entry).split())
        toks = tuple(t for t in toks if t)
        if toks:
            phrases.add(toks)
    by_len = sorted(phrases, key=len, reverse=True)

    norm = [_norm_word(w.get("text", "")) for w in words]
    flags = [False] * len(words)
    i = 0
    while i < len(words):
        for ph in by_len:
            L = len(ph)
            if tuple(norm[i:i + L]) == ph:
                for j in range(i, i + L):
                    flags[j] = True
                i += L
                break
        else:
            i += 1
    return flags


def plan_speech_cleanup(
    words: list[dict],
    span_start: float,
    span_end: float,
    params: dict | None = None,
) -> dict:
    """Plan how to tighten the [span_start, span_end] source span.

    `words` are the aligned words overlapping the span, each
    `{text, start|local_start, end|local_end, score?}` in source-clip seconds.

    Returns:
        {
          "keep":    [{"start", "end"}, ...]  # spans to retain, in order
          "removed": [{"reason", "start", "end", "text"?}, ...]
          "duration_before", "duration_after", "saved",
          "kept_words": int, "filler_words": int,
        }

    `reason` is one of "filler", "silence", "lead_silence", "tail_silence".
    If the span contains no speech (no words, or none after filler removal) the
    whole span is kept untouched — deliberate non-speech is never compressed.
    """
    p = {**SPEECH_CLEANUP_DEFAULTS, **(params or {})}
    span_start, span_end = float(span_start), float(span_end)

    def _clamp(x: float) -> float:
        return max(span_start, min(span_end, x))

    # Words actually inside the span, in time order.
    inside: list[dict] = []
    for w in words:
        s, e = _word_bounds(w)
        if e > span_start and s < span_end:
            inside.append(w)
    inside.sort(key=lambda w: _word_bounds(w)[0])

    untouched = {
        "keep": [{"start": round(span_start, 3), "end": round(span_end, 3)}],
        "removed": [],
        "duration_before": round(span_end - span_start, 3),
        "duration_after": round(span_end - span_start, 3),
        "saved": 0.0,
        "kept_words": 0,
        "filler_words": 0,
    }
    if not inside:
        return untouched

    flags = (
        _filler_flags(inside, p.get("filler_lexicon"))
        if p.get("remove_fillers")
        else [False] * len(inside)
    )
    filler_words = [w for w, f in zip(inside, flags) if f]
    speech = [w for w, f in zip(inside, flags) if not f]
    if not speech:
        # Nothing but fillers (or nothing): treat as non-speech, leave alone.
        return untouched

    min_score = p.get("min_score")
    low_pad = float(p.get("low_score_pad") or 0.0)

    def _padded(w: dict) -> tuple[float, float]:
        s, e = _word_bounds(w)
        ps, pe = float(p["pad_start"]), float(p["pad_end"])
        if min_score is not None and w.get("score") is not None and (
            float(w["score"]) < float(min_score)
        ):
            ps += low_pad
            pe += low_pad
        return _clamp(s - ps), _clamp(e + pe)

    max_gap = float(p["max_gap"])
    collapse_to = max(0.0, float(p["collapse_to"]))
    protect = p.get("protect_gap_over")
    protect = float(protect) if protect is not None else None
    min_removed = max(0.0, float(p["min_removed"]))

    # Build keep intervals word by word, collapsing the gaps between them.
    keep: list[list[float]] = []
    for w in speech:
        s, e = _padded(w)
        if not keep:
            keep.append([s, e])
            continue
        gap = s - keep[-1][1]
        if gap <= max_gap or (protect is not None and gap > protect):
            # Short pause, or an intentional long beat: keep it continuous.
            keep[-1][1] = max(keep[-1][1], e)
        else:
            # Collapse: retain `collapse_to` of breath, cut the rest — but only
            # if the cut actually reclaims enough to be worth a jump.
            new_end = min(keep[-1][1] + collapse_to, s)
            if (s - new_end) >= min_removed:
                keep[-1][1] = new_end
                keep.append([s, e])
            else:
                keep[-1][1] = max(keep[-1][1], e)

    # Head / tail dead air.
    if not p.get("trim_lead"):
        keep[0][0] = span_start
    if not p.get("trim_tail"):
        keep[-1][1] = span_end

    keep_rounded = [
        {"start": round(s, 3), "end": round(e, 3)} for s, e in keep if e > s
    ]

    # Removed = the span minus what we kept, each piece labelled by cause.
    removed: list[dict] = []
    cursor = span_start
    for seg in keep:
        if seg[0] > cursor + 1e-6:
            removed.append(_label_removed(
                cursor, seg[0], span_start, span_end, filler_words
            ))
        cursor = max(cursor, seg[1])
    if span_end > cursor + 1e-6:
        removed.append(_label_removed(
            cursor, span_end, span_start, span_end, filler_words
        ))

    after = sum(seg["end"] - seg["start"] for seg in keep_rounded)
    before = span_end - span_start
    return {
        "keep": keep_rounded,
        "removed": removed,
        "duration_before": round(before, 3),
        "duration_after": round(after, 3),
        "saved": round(before - after, 3),
        "kept_words": len(speech),
        "filler_words": len(filler_words),
    }


def _label_removed(
    start: float,
    end: float,
    span_start: float,
    span_end: float,
    filler_words: list[dict],
) -> dict:
    """Classify one removed region by what it overlaps."""
    hits = []
    for w in filler_words:
        ws, we = _word_bounds(w)
        if we > start and ws < end:
            hits.append((w.get("text") or "").strip())
    out = {"start": round(start, 3), "end": round(end, 3)}
    if hits:
        out["reason"] = "filler"
        out["text"] = " ".join(t for t in hits if t)
    elif abs(start - span_start) < 1e-6:
        out["reason"] = "lead_silence"
    elif abs(end - span_end) < 1e-6:
        out["reason"] = "tail_silence"
    else:
        out["reason"] = "silence"
    return out


# ---------------------------------------------------------------------------
# VAD-fused speech cleanup (the validated silence crop)
# ---------------------------------------------------------------------------
#
# Word timings alone make sloppy cuts: WhisperX onsets run ~0.1-0.7 s early
# (worst right after a pause) and the gaps between its contiguous word spans
# don't correspond to real silence. Silero VAD nails the speech/silence edge
# but clips unvoiced consonants (a leading /s/) and occasionally drops quiet
# speech. So we fuse three signals — keep a moment if VAD calls it speech OR an
# energy burst (from the stored waveform peaks) coincides with a transcript
# word; cut only where all three agree there's nothing. Constants below were
# fixed by the experiments in experiments/vad_align (validated by ear).

VAD_CLEANUP_DEFAULTS: dict = {
    "tau": 0.5,             # VAD onset threshold
    "tau_off": 0.35,        # VAD release threshold (hysteresis)
    "energy_margin_db": 12.0,   # energy speech = this far above the noise floor
    "pad": 0.05,            # speech-side margin so onsets/offsets aren't clipped
    "min_silence": 0.25,    # gaps shorter than this are kept (natural micro-pauses)
    "min_removed": 0.20,    # don't bother cutting a gap that reclaims less than this
    "protect_gap_over": None,   # never cut a deliberate pause longer than this
    "remove_fillers": False,    # silence crop only by default; fillers are opt-in
    "filler_lexicon": None,
}


def _merge_iv(ivs: list[tuple[float, float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted(ivs):
        if out and s <= out[-1][1]:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def _speech_gate(
    prob: list[float], hop: float, threshold: float, neg_threshold: float,
    min_speech: float, min_silence: float, total: float | None,
) -> list[tuple[float, float]]:
    """Hysteresis speech gate over a probability/binary curve (seconds out).

    Mirrors audio_analysis.intervals_from_curve, re-implemented here to keep
    timeline.py self-contained (stdlib only, importable in tests by path)."""
    if not prob:
        return []
    n = len(prob)
    min_sil_win = max(1, round(min_silence / hop)) if hop > 0 else 1
    raw: list[tuple[int, int]] = []
    triggered = False
    start = temp_end = 0
    for i, pp in enumerate(prob):
        if pp >= threshold:
            temp_end = 0
            if not triggered:
                triggered, start = True, i
        elif triggered and pp < neg_threshold:
            if not temp_end:
                temp_end = i
            if i - temp_end >= min_sil_win:
                raw.append((start, temp_end))
                triggered, temp_end = False, 0
    if triggered:
        raw.append((start, n))
    out: list[tuple[float, float]] = []
    for s, e in raw:
        if (e - s) * hop < min_speech:
            continue
        st, en = s * hop, e * hop
        if total is not None:
            en = min(en, total)
        if en > st:
            out.append((st, en))
    return out


def _energy_speech(
    peaks: list[float], hop: float, margin_db: float, min_silence: float,
    total: float | None,
) -> list[tuple[float, float]]:
    """Speech bursts from the stored waveform peak envelope, thresholded
    relative to the clip's own noise floor (10th-percentile peak)."""
    if not peaks:
        return []
    db = [20 * math.log10(p + 1e-4) for p in peaks]
    floor = sorted(db)[int(0.10 * len(db))]
    thr = floor + margin_db
    pseudo = [1.0 if d > thr else 0.0 for d in db]
    return _speech_gate(pseudo, hop, 0.5, 0.5, 0.05, min_silence, total)


def _overlaps_word(s: float, e: float, words: list[dict], slack: float = 0.15) -> bool:
    for w in words:
        ws, we = _word_bounds(w)
        if we + slack >= s and ws - slack <= e:
            return True
    return False


def fused_speech_keep(
    vad_prob: list[float], vad_hop: float, peaks: list[float], peaks_hop: float,
    words: list[dict], params: dict | None = None,
) -> list[tuple[float, float]]:
    """Clip-local KEEP intervals from the three-signal fusion (see notes above)."""
    p = {**VAD_CLEANUP_DEFAULTS, **(params or {})}
    total = (len(vad_prob) * vad_hop) if vad_prob else (
        len(peaks) * peaks_hop if peaks else 0.0)
    pad = float(p["pad"])
    min_sil = float(p["min_silence"])
    vad = _speech_gate(vad_prob, vad_hop, float(p["tau"]), float(p["tau_off"]),
                       0.10, min_sil, total)
    eng = _energy_speech(peaks, peaks_hop, float(p["energy_margin_db"]), min_sil, total)
    eng = [iv for iv in eng if _overlaps_word(iv[0], iv[1], words)]
    padded = [(max(0.0, s - pad), min(total, e + pad)) for s, e in (vad + eng)]
    keep = _merge_iv(padded)
    # Re-close gaps shorter than min_silence so micro-pauses survive.
    closed: list[list[float]] = []
    for s, e in keep:
        if closed and s - closed[-1][1] < min_sil:
            closed[-1][1] = e
        else:
            closed.append([s, e])
    return [(s, e) for s, e in closed]


def plan_speech_cleanup_vad(
    words: list[dict],
    vad_prob: list[float], vad_hop: float,
    peaks: list[float], peaks_hop: float,
    span_start: float, span_end: float,
    params: dict | None = None,
) -> dict:
    """VAD-fused analogue of plan_speech_cleanup over [span_start, span_end].

    Same return shape. Silence comes from the three-signal fusion (not word
    gaps); optional filler removal subtracts filler-word spans from the kept
    speech. Falls back to keeping the whole span when there's no speech.
    """
    p = {**VAD_CLEANUP_DEFAULTS, **(params or {})}
    span_start, span_end = float(span_start), float(span_end)
    before = span_end - span_start

    untouched = {
        "keep": [{"start": round(span_start, 3), "end": round(span_end, 3)}],
        "removed": [], "duration_before": round(before, 3),
        "duration_after": round(before, 3), "saved": 0.0,
        "kept_words": 0, "filler_words": 0,
    }

    speech = fused_speech_keep(vad_prob, vad_hop, peaks, peaks_hop, words, p)
    # Restrict to the item's span.
    keep = [[max(span_start, s), min(span_end, e)]
            for s, e in speech if e > span_start and s < span_end]
    keep = [seg for seg in keep if seg[1] - seg[0] > 1e-3]
    if not keep:
        return untouched

    # Words inside the span (for counts + filler removal).
    inside = [w for w in words if _word_bounds(w)[1] > span_start
              and _word_bounds(w)[0] < span_end]
    flags = (_filler_flags(inside, p.get("filler_lexicon"))
             if p.get("remove_fillers") else [False] * len(inside))
    filler_words = [w for w, f in zip(inside, flags) if f]

    # Subtract filler spans from the kept speech (creating jump cuts), but only
    # when the reclaimed slice is worth a cut.
    min_removed = float(p["min_removed"])
    for fw in filler_words:
        fs, fe = _word_bounds(fw)
        if fe - fs < min_removed:
            continue
        next_keep: list[list[float]] = []
        for s, e in keep:
            lo, hi = max(s, fs), min(e, fe)
            if hi <= lo:
                next_keep.append([s, e])
                continue
            if lo - s > 1e-3:
                next_keep.append([s, lo])
            if e - hi > 1e-3:
                next_keep.append([hi, e])
        keep = next_keep

    # Drop cuts that reclaim less than min_removed (re-close them).
    closed: list[list[float]] = []
    for s, e in keep:
        if closed and s - closed[-1][1] < min_removed:
            closed[-1][1] = e
        else:
            closed.append([s, e])
    keep = closed

    keep_rounded = [{"start": round(s, 3), "end": round(e, 3)} for s, e in keep]
    removed: list[dict] = []
    cursor = span_start
    for seg in keep:
        if seg[0] > cursor + 1e-6:
            removed.append(_label_removed(cursor, seg[0], span_start, span_end,
                                          filler_words))
        cursor = max(cursor, seg[1])
    if span_end > cursor + 1e-6:
        removed.append(_label_removed(cursor, span_end, span_start, span_end,
                                      filler_words))

    after = sum(seg["end"] - seg["start"] for seg in keep_rounded)
    return {
        "keep": keep_rounded, "removed": removed,
        "duration_before": round(before, 3), "duration_after": round(after, 3),
        "saved": round(before - after, 3),
        "kept_words": len(inside) - len(filler_words),
        "filler_words": len(filler_words),
    }


def _words_for_item(
    item: dict,
    words: list[dict] | None,
    words_by_clip_id: dict | None,
) -> list[dict]:
    """Pick the aligned words that belong to one clip item.

    Cross-project clips (carrying a global `clip_id`) take their words from
    `words_by_clip_id[clip_id]` — already scoped to exactly that clip's owning
    project, so a filename shared by two clips in different projects can't bleed
    across (the filename-collision trap). Local clips (bare `source` filename,
    no `clip_id`) keep the legacy behavior: filter the flat home-project `words`
    list by `source` filename. See docs/cross_project_editing.md (Tier 2).
    """
    cid = item.get("clip_id")
    if cid and words_by_clip_id is not None and cid in words_by_clip_id:
        # Authoritative per-clip words — already the right project's, so use as
        # is. We do NOT re-filter by `source` filename: a foreign clip's words
        # may be tagged with that clip's real filename, which can differ from
        # the human-readable label `source` carried on the timeline item.
        return list(words_by_clip_id.get(cid) or [])
    src = item.get("source")
    return [w for w in (words or []) if w.get("source") in (None, src)]


def expand_clean_speech(
    timeline: dict,
    item_id: str,
    words: list[dict],
    params: dict | None = None,
    audio: dict | None = None,
    words_by_clip_id: dict | None = None,
) -> tuple[dict, dict]:
    """Replace one clip item with tight jump-cut sub-items per the cleanup plan.

    Pure: returns a NEW timeline (the input is not mutated) plus the
    `plan_speech_cleanup` result so callers can report what was removed.

    `words` are aligned words (with `source` + local times); for a local clip
    only those whose `source` matches the item are used. For a cross-project
    clip (one carrying a global `clip_id`), per-clip words are taken from
    `words_by_clip_id[clip_id]` instead — already scoped to that clip's owning
    project, which avoids the filename-collision trap of filtering the flat
    home-project list by a (possibly shared) filename. See
    docs/cross_project_editing.md (Tier 2).

    `audio` is this clip's stored audio analysis, already resolved by the caller
    for the target clip (by clip_id when foreign, else by filename); when
    present the VAD-fused silence crop is used. The first sub-item inherits the
    original item's `transition_in`; subsequent ones are hard cuts unless
    `params["join"]` supplies a crossfade transition to soften the seams. All
    other per-clip fields (speed, mute, audio_fx, note) carry over unchanged.
    """
    result = copy.deepcopy(timeline)
    idx, item = _find_video(result, item_id)
    _require_clip(item, "clean_speech")

    span_start = float(item["src_start"])
    span_end = float(item["src_end"])
    relevant = _words_for_item(item, words, words_by_clip_id)

    # Prefer the VAD-fused plan when this clip's audio analysis is available
    # (accurate silence edges); fall back to word-gap timing otherwise.
    try:
        if audio and audio.get("vad_prob"):
            plan = plan_speech_cleanup_vad(
                relevant, audio.get("vad_prob") or [], float(audio.get("vad_hop") or 0.032),
                audio.get("peaks") or [], float(audio.get("peaks_hop") or 0.02),
                span_start, span_end, params,
            )
        else:
            plan = plan_speech_cleanup(relevant, span_start, span_end, params)
    except (ValueError, TypeError) as exc:
        raise TimelineError(f"clean_speech on {item_id}: bad params ({exc})")

    keep = plan["keep"]
    if not keep:
        raise TimelineError(f"clean_speech on {item_id}: nothing left to keep")

    join = (params or {}).get("join")

    items = video_items(result)
    new_items: list[dict] = []
    for k, seg in enumerate(keep):
        sub = copy.deepcopy(item)
        sub["src_start"] = seg["start"]
        sub["src_end"] = seg["end"]
        if k == 0:
            sub["id"] = item["id"]  # keep the original id on the first piece
            sub["transition_in"] = item.get("transition_in")
        else:
            sub["transition_in"] = join if isinstance(join, dict) else None
        new_items.append(sub)

    # Fresh ids for the 2nd..Nth pieces. The original id is still present, so
    # _new_id picks numbers strictly above it — no collisions.
    items[idx:idx + 1] = new_items
    for sub in new_items[1:]:
        sub["id"] = _new_id(result, "v")

    return result, plan


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
    music_path: str | None = None,
) -> dict:
    """Compile a timeline into ffmpeg invocation pieces.

    resolve_source(item) -> local file path for a clip item. The callback is
    passed the whole clip item so it can resolve cross-project clips by the
    optional `clip_id` (authoritative, global) before falling back to the
    `source` filename (scoped to the story's home project). See
    docs/cross_project_editing.md.

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

        path = resolve_source(item)
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

    # Optional external music track laid under the footage (see _validate_music).
    # One extra input, seeked to song_start and trimmed/padded to the output
    # length so a short song can't truncate the video's audio. `scratch_gain_db`
    # None → replace (clip audio muted); a number → duck clip audio and mix.
    music = timeline.get("music")
    if music and music_path:
        midx = in_idx  # next free input index (== len(inputs))
        inputs.append([
            "-ss", f"{float(music['song_start']):.3f}",
            "-t", f"{out_dur:.3f}", "-i", str(music_path),
        ])
        gain = float(music.get("gain_db") or 0.0)
        gvol = f",volume={gain}dB" if gain else ""
        parts.append(
            f"[{midx}:a]asetpts=PTS-STARTPTS,aresample={sr},"
            f"aformat=channel_layouts=stereo,apad,"
            f"atrim=duration={out_dur:.3f}{gvol}[amus]"
        )
        scratch = music.get("scratch_gain_db")
        if scratch is None:
            parts.append(f"[{ca}]volume=0[ascr]")  # replace: mute clip audio
        else:
            parts.append(f"[{ca}]volume={float(scratch)}dB[ascr]")  # duck + mix
        parts.append(
            "[ascr][amus]amix=inputs=2:normalize=0:duration=first[aout]"
        )
    else:
        parts.append(f"[{ca}]anull[aout]")

    return {
        "inputs": inputs,
        "filter_complex": ";".join(parts),
        "text_files": text_files,
        "duration": round(out_dur, 3),
    }
