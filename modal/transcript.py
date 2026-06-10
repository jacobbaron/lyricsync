"""Pure transcript formatting + quote→timestamp matching.

No Modal/FastAPI/network deps — stdlib only — so it imports cleanly in tests.
modal/app.py imports these helpers; Modal automounts this local module on deploy.

The story-generation flow gives Claude the transcript with no timestamps; Claude
returns verbatim quotes, and the functions here map each quote back to a precise
word-level timestamp range deterministically.
"""

from __future__ import annotations

import difflib
import re

# Minimum fraction of a quote's tokens that must align with the source
# transcript for a match to be accepted. Below this the caller fails loud.
MATCH_MIN_SCORE = 0.6
_TOKEN_RE = re.compile(r"[a-z0-9']+")


def _visual_line(hl: dict) -> str:
    """Format one visual highlight as a bracketed annotation line.

    Square brackets + the 'visual' prefix mark these as non-speech: the system
    prompt tells Claude annotations are never quotable.
    """
    extra = ""
    if hl.get("expression"):
        extra += f" — face: {hl['expression']}"
    if hl.get("tone"):
        extra += f" — tone: {hl['tone']}"
    kind = hl.get("kind") or "moment"
    desc = (hl.get("description") or "").strip()
    return f"[visual {float(hl['time']):.0f}s — {kind}: {desc}{extra}]"


def format_transcript(
    words: list[dict], visuals_by_source: dict[str, dict] | None = None
) -> str:
    """Format the merged word list as plain text for Claude — no timestamps.

    Groups each source's words into paragraphs (split on gaps > 1.5 s) under a
    "=== filename ===" header, sources ordered by first appearance. Claude
    quotes verbatim text; resolve_segments maps quotes back to timestamps.

    visuals_by_source optionally maps a source filename to its parsed visual
    track ({summary, highlights: [{time, kind, description, expression?,
    tone?}]}, clip-local seconds — see visual.parse_visual_response). When
    present, the section opens with a "[visual context: …]" summary line and
    highlight beats are interleaved with the speech paragraphs at their
    position in time, so Claude can pick moments with strong visual energy.
    Output is unchanged for sources without visuals.
    """
    GAP_S = 1.5

    by_source: dict[str, list[dict]] = {}
    for w in words:
        src = w.get("source", "?")
        by_source.setdefault(src, []).append(w)

    ordered = sorted(
        by_source.items(),
        key=lambda kv: min(w["global_start"] for w in kv[1]),
    )

    sections: list[str] = []
    for src, ws in ordered:
        ws = sorted(ws, key=lambda x: x["global_start"])

        # Paragraphs as (local_start, local_end, text) — local times so they
        # can be merged with the clip-local visual track.
        paragraphs: list[tuple[float, float, str]] = []
        cur_words: list[str] = []
        cur_local_start = ws[0]["local_start"]
        cur_local_end = ws[0]["local_end"]
        cur_end_global = ws[0]["global_end"]

        for w in ws:
            gap = w["global_start"] - cur_end_global
            if cur_words and gap > GAP_S:
                paragraphs.append(
                    (cur_local_start, cur_local_end, " ".join(cur_words))
                )
                cur_words = []
                cur_local_start = w["local_start"]
            cur_words.append((w.get("text") or "").strip())
            cur_local_end = w["local_end"]
            cur_end_global = w["global_end"]

        if cur_words:
            paragraphs.append((cur_local_start, cur_local_end, " ".join(cur_words)))

        visual = (visuals_by_source or {}).get(src) or {}
        events = sorted(
            (h for h in (visual.get("highlights") or []) if h.get("time") is not None),
            key=lambda h: float(h["time"]),
        )

        blocks: list[str] = []
        summary = (visual.get("summary") or "").strip()
        if summary:
            blocks.append(f"[visual context: {summary}]")

        ei = 0
        for local_start, local_end, text in paragraphs:
            # Beats before the paragraph starts go above it; beats during it go
            # right after (never inside — paragraphs stay verbatim-quotable).
            while ei < len(events) and float(events[ei]["time"]) <= local_start:
                blocks.append(_visual_line(events[ei]))
                ei += 1
            blocks.append(text)
            while ei < len(events) and float(events[ei]["time"]) <= local_end:
                blocks.append(_visual_line(events[ei]))
                ei += 1
        while ei < len(events):
            blocks.append(_visual_line(events[ei]))
            ei += 1

        sections.append(f"=== {src} ===\n" + "\n\n".join(blocks))

    return "\n\n".join(sections)


def normalize_tokens(text: str) -> list[str]:
    """Lowercase word tokens, punctuation stripped, for fuzzy alignment."""
    return _TOKEN_RE.findall((text or "").lower())


def build_token_index(words: list[dict]) -> tuple[list[str], list[int]]:
    """Flatten a word list to normalized tokens with a parallel word-index list."""
    tokens: list[str] = []
    word_idx: list[int] = []
    for i, w in enumerate(words):
        for tok in normalize_tokens(w.get("text") or ""):
            tokens.append(tok)
            word_idx.append(i)
    return tokens, word_idx


def match_quote(
    quote: str, words: list[dict], tokens: list[str], word_idx: list[int]
) -> dict | None:
    """Map a verbatim quote to a {start, end, text, score} span via token alignment.

    Uses difflib to find the best-aligned run of source tokens, then takes the
    first and last matched words' local timestamps. Returns None if nothing
    aligned at all.
    """
    q_tokens = normalize_tokens(quote)
    if not q_tokens or not tokens:
        return None

    sm = difflib.SequenceMatcher(a=tokens, b=q_tokens, autojunk=False)
    blocks = [b for b in sm.get_matching_blocks() if b.size > 0]
    if not blocks:
        return None

    first_tok = blocks[0].a
    last_tok = blocks[-1].a + blocks[-1].size - 1
    matched = sum(b.size for b in blocks)
    score = matched / len(q_tokens)

    first_word = word_idx[first_tok]
    last_word = word_idx[last_tok]
    text = " ".join(
        (words[i].get("text") or "").strip()
        for i in range(first_word, last_word + 1)
    )
    return {
        "start": words[first_word]["local_start"],
        "end": words[last_word]["local_end"],
        "text": text,
        "score": score,
    }


def build_source_index(
    words: list[dict],
) -> dict[str, tuple[list[dict], list[str], list[int]]]:
    """Group words by source (sorted by global_start) and build token indexes."""
    by_source: dict[str, list[dict]] = {}
    for w in words:
        by_source.setdefault(w.get("source", "?"), []).append(w)
    index: dict[str, tuple[list[dict], list[str], list[int]]] = {}
    for src, ws in by_source.items():
        ws = sorted(ws, key=lambda x: x["global_start"])
        index[src] = (ws, *build_token_index(ws))
    return index


def resolve_segments(
    segments: list[dict],
    index_by_source: dict[str, tuple[list[dict], list[str], list[int]]],
) -> list[dict]:
    """Resolve {source, quote} segments to {source, start, end, text} ranges.

    Fails loud (raises ValueError) on an unknown source or a low-confidence
    match, so the worker marks the project errored with a specific message.
    """
    ranges: list[dict] = []
    for seg in segments:
        src = seg.get("source", "")
        quote = seg.get("quote", "")
        if src not in index_by_source:
            raise ValueError(
                f"quote references unknown source '{src}' — quote: {quote[:80]!r}"
            )
        words, tokens, word_idx = index_by_source[src]
        m = match_quote(quote, words, tokens, word_idx)
        if m is None or m["score"] < MATCH_MIN_SCORE:
            score = m["score"] if m else 0.0
            raise ValueError(
                f"could not match quote in {src} "
                f"(confidence {score:.0%}) — quote: {quote[:80]!r}"
            )
        ranges.append({
            "source": src,
            "start": round(m["start"], 2),
            "end": round(m["end"], 2),
            "text": m["text"],
            "quote": quote,
        })
    return ranges


def stories_as_text(stories: list[dict]) -> str:
    """Render story dicts as a readable text block for conversation history.

    Shows the verbatim quotes (not timestamps) so the format Claude sees in
    history matches the format it produces.
    """
    parts: list[str] = []
    for i, s in enumerate(stories, 1):
        seg_lines: list[str] = []
        for r in (s.get("ranges") or []):
            txt = (r.get("text") or "").strip()
            if not txt:
                snippet = "(segment)"
            elif len(txt) <= 120:
                snippet = txt
            else:
                snippet = txt[:117] + "…"
            seg_lines.append(f"  - [{r['source']}] {snippet}")
        parts.append(
            f"Option {i}: \"{s['title']}\"\n"
            f"{s['description']}\n"
            + "\n".join(seg_lines)
        )
    return "\n\n".join(parts)
