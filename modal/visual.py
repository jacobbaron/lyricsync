"""Pure helpers for Gemini visual-analysis output.

No Modal/SDK/network deps — stdlib only — so it imports cleanly in tests, the
same way transcript.py does. modal/app.py imports these; Modal mounts this local
module on deploy.

The visual-analysis flow sends a clip to Gemini and asks for a timestamped
description of what's on screen (shot/scene segments) plus point-in-time
"highlight" beats (reactions, gestures, actions, pauses). Gemini returns JSON;
the helpers here parse it and normalize every timestamp to float seconds
measured from the start of the clip, matching the clip-local timebase the render
worker trims on.
"""

from __future__ import annotations

import json
import math
import re

# Shot/scene segment and highlight "kind" vocabularies are advisory — Gemini is
# asked to prefer these but free-text is tolerated and passed through as-is.
SHOT_KINDS = ("wide", "medium", "close", "other")
HIGHLIGHT_KINDS = (
    "reaction", "gesture", "action", "pause", "expression", "movement", "other",
)


def build_prompt(
    duration_secs: float | None,
    strategy: str = "default",
    transcript_text: str | None = None,
) -> str:
    """Build the Gemini instruction asking for timestamped visual JSON.

    Timestamps must be seconds (numbers) measured from the start of the clip so
    they line up with the render worker's clip-local trim points.

    strategy:
      "default"     — summary + segments + highlights (describe what's on screen,
                      ignore the words spoken).
      "editorial"   — same, plus a `suggested_clips` array of ready-to-render
                      {start, end, reason} moments the model would cut for a short.
      "audio_aware" — use BOTH the audio and the video: describe the on-screen
                      action AND read the speaker's tone/emotion, with a detailed
                      `expression` field on every highlight.
      "transcript"  — like audio_aware, but the aligned transcript is supplied as
                      ground-truth text (`transcript_text`) so the model can relate
                      what is shown to what is said without mis-hearing words.

    Gemini ingests the audio track of an uploaded video by default, so "default"
    only *asks* it to ignore speech; "audio_aware"/"transcript" lean into it.
    """
    dur = (
        f"The clip is about {duration_secs:.1f} seconds long. "
        if duration_secs and duration_secs > 0
        else ""
    )
    editorial = strategy == "editorial"
    audio = strategy in ("audio_aware", "transcript")
    with_transcript = strategy == "transcript"

    # Intro framing differs by whether we want a pure-visual or audio+visual read.
    if audio:
        intro = (
            "You are a video editor's assistant. Watch this single, unedited "
            "video clip using BOTH its audio and its visuals. Describe what is "
            "happening on screen AND how the person sounds and feels — their "
            f"tone, emotion, and energy. {dur}"
        )
    else:
        intro = (
            "You are a video editor's assistant. Watch this single, unedited "
            "video clip and describe what is happening ON SCREEN — not the words "
            f"spoken. {dur}"
        )

    # When the transcript is supplied, give it as ground truth so the model
    # doesn't have to (mis)transcribe the audio itself.
    transcript_block = ""
    if with_transcript and transcript_text:
        transcript_block = (
            "\n\nHere is the exact, time-aligned transcript of what is said in "
            "this clip (timestamps are seconds from the clip start). Treat it as "
            "ground truth for the words; use the audio only for tone/emotion and "
            "the video for everything visual:\n"
            f"{transcript_text.strip()}\n"
        )

    # highlights carry a richer `expression` field in the audio/transcript modes
    # so we actually capture the speaker's face and delivery, not just "smiles".
    if audio:
        highlight_schema = (
            '  "highlights": [\n'
            '    {"time": <seconds>, '
            '"kind": "reaction|gesture|action|pause|expression|movement|other", '
            '"description": "the visual beat", '
            '"expression": "detailed read of the face/emotion: brow, eyes, mouth, '
            'gaze direction, intensity (subtle|moderate|strong)", '
            '"tone": "how the voice sounds at this moment, if speaking"}\n'
            "  ]\n"
        )
    else:
        highlight_schema = (
            '  "highlights": [\n'
            '    {"time": <seconds>, '
            '"kind": "reaction|gesture|action|pause|expression|movement|other", '
            '"description": "the visual beat"}\n'
            "  ]\n"
        )

    suggested_schema = (
        '  ,"suggested_clips": [\n'
        '    {"start": <seconds>, "end": <seconds>, '
        '"reason": "why this makes a strong short-form clip"}\n'
        "  ]\n"
        if editorial
        else ""
    )
    suggested_rule = (
        "- suggested_clips: propose 2-4 self-contained moments (each a few "
        "seconds to ~30s) that would make strong standalone short-form clips, "
        "judged on what is VISUALLY happening.\n"
        if editorial
        else ""
    )

    if audio:
        describe_rule = (
            "- Describe what you SEE (people, framing, motion, setting) and, for "
            "highlights, give a detailed read of the facial expression and vocal "
            "tone — capture the actual emotion and its intensity, not just "
            '"smiles" or "looks up".\n'
        )
    else:
        describe_rule = (
            "- Describe what you SEE (people, framing, motion, expressions, "
            "setting), not what is said.\n"
        )

    return (
        f"{intro}"
        f"{transcript_block}\n\n"
        "Return ONLY a JSON object (no markdown, no code fences) with this shape:\n"
        "{\n"
        '  "summary": "one or two sentences on what this clip shows",\n'
        '  "segments": [\n'
        '    {"start": <seconds>, "end": <seconds>, '
        '"shot": "wide|medium|close|other", "description": "what is visible"}\n'
        "  ],\n"
        f"{highlight_schema}"
        f"{suggested_schema}"
        "}\n\n"
        "Rules:\n"
        "- All timestamps are SECONDS (numbers) from the start of the clip, "
        "e.g. 12.5 — not MM:SS strings. Never collapse the timeline into "
        "fractions of a second; use the real clip duration above.\n"
        "- segments should tile the clip in order, splitting whenever the "
        "framing, subject, or activity changes.\n"
        "- highlights are the few moments a human would actually cut on: a "
        "genuine reaction, a laugh, a gesture, an action beat, eye contact with "
        "the camera, or a notable pause/stillness.\n"
        f"{suggested_rule}"
        f"{describe_rule}"
        "- If nothing notable happens, return an empty highlights array."
    )


def normalize_timestamp(value: object) -> float | None:
    """Coerce a Gemini timestamp into float seconds, or None if unparseable.

    Accepts numbers, "12.6", "12.6s", "MM:SS", and "HH:MM:SS". We instruct the
    model to emit plain seconds, but tolerate the clock formats it sometimes
    falls back to so a single odd value doesn't sink a whole response.
    """
    if isinstance(value, bool):  # bool is an int subclass — reject explicitly
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) and value >= 0 else None
    if not isinstance(value, str):
        return None

    s = value.strip().lower().rstrip("s").strip()
    if not s:
        return None

    if ":" in s:
        parts = s.split(":")
        try:
            nums = [float(p) for p in parts]
        except ValueError:
            return None
        secs = 0.0
        for n in nums:  # H:M:S or M:S — accumulate left-to-right
            secs = secs * 60 + n
        return secs if math.isfinite(secs) and secs >= 0 else None

    if not re.fullmatch(r"\d+(\.\d+)?", s):
        return None
    f = float(s)
    return f if math.isfinite(f) and f >= 0 else None


def _strip_code_fence(text: str) -> str:
    """Remove a leading/trailing ```json ... ``` fence if Gemini added one."""
    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z0-9]*\s*", "", t)
        t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_visual_response(
    raw_text: str, duration_secs: float | None = None
) -> dict:
    """Parse Gemini's JSON into a normalized visual track.

    Returns {"summary", "segments", "highlights"} with every timestamp coerced
    to float seconds. Invalid rows (unparseable or non-positive-length segments)
    are dropped rather than failing the whole clip. Raises ValueError only when
    the payload isn't valid JSON at all.
    """
    try:
        data = json.loads(_strip_code_fence(raw_text))
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"Gemini did not return valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Gemini JSON was not an object")

    cap = duration_secs if (duration_secs and duration_secs > 0) else None

    segments: list[dict] = []
    for seg in data.get("segments", []) or []:
        if not isinstance(seg, dict):
            continue
        start = normalize_timestamp(seg.get("start"))
        end = normalize_timestamp(seg.get("end"))
        if start is None or end is None:
            continue
        if cap is not None:
            end = min(end, cap)
        if end <= start:
            continue
        segments.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "shot": str(seg.get("shot") or "other").strip().lower(),
            "description": str(seg.get("description") or "").strip(),
        })
    segments.sort(key=lambda s: s["start"])

    highlights: list[dict] = []
    for hl in data.get("highlights", []) or []:
        if not isinstance(hl, dict):
            continue
        t = normalize_timestamp(hl.get("time"))
        if t is None:
            continue
        if cap is not None and t > cap:
            t = cap
        beat = {
            "time": round(t, 2),
            "kind": str(hl.get("kind") or "other").strip().lower(),
            "description": str(hl.get("description") or "").strip(),
        }
        # Audio-aware strategies add a detailed face/emotion read and vocal tone;
        # pass them through only when present so default output is unchanged.
        if hl.get("expression"):
            beat["expression"] = str(hl["expression"]).strip()
        if hl.get("tone"):
            beat["tone"] = str(hl["tone"]).strip()
        highlights.append(beat)
    highlights.sort(key=lambda h: h["time"])

    suggested: list[dict] = []
    for clip in data.get("suggested_clips", []) or []:
        if not isinstance(clip, dict):
            continue
        start = normalize_timestamp(clip.get("start"))
        end = normalize_timestamp(clip.get("end"))
        if start is None or end is None:
            continue
        if cap is not None:
            end = min(end, cap)
        if end <= start:
            continue
        suggested.append({
            "start": round(start, 2),
            "end": round(end, 2),
            "reason": str(clip.get("reason") or "").strip(),
        })
    suggested.sort(key=lambda c: c["start"])

    return {
        "summary": str(data.get("summary") or "").strip(),
        "segments": segments,
        "highlights": highlights,
        "suggested_clips": suggested,
    }


def format_visual_track(visual: dict) -> str:
    """Render a parsed visual track as readable text (for logs / future prompts).

    Mirrors the role of transcript.format_transcript: a compact, timestamped
    textual view of the on-screen action that a downstream picker could read
    alongside the spoken transcript.
    """
    lines: list[str] = []
    summary = visual.get("summary")
    if summary:
        lines.append(f"Summary: {summary}")
    for seg in visual.get("segments", []):
        lines.append(
            f"[{seg['start']:.1f}-{seg['end']:.1f}s | {seg.get('shot', 'other')}] "
            f"{seg.get('description', '')}"
        )
    for hl in visual.get("highlights", []):
        extra = ""
        if hl.get("expression"):
            extra += f" — face: {hl['expression']}"
        if hl.get("tone"):
            extra += f" — tone: {hl['tone']}"
        lines.append(
            f"  * {hl['time']:.1f}s ({hl.get('kind', 'other')}): "
            f"{hl.get('description', '')}{extra}"
        )
    for clip in visual.get("suggested_clips", []):
        lines.append(
            f"[suggested {clip['start']:.1f}-{clip['end']:.1f}s] "
            f"{clip.get('reason', '')}"
        )
    return "\n".join(lines)
