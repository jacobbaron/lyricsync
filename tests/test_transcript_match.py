"""Tests for transcript formatting and quote→timestamp matching.

Loads modal/transcript.py by path: modal/ is not an importable package, and the
name collides with the pip `modal` package, so importlib-from-path is cleanest.
The module is stdlib-only, so no heavy deps are needed.
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "transcript.py"
_spec = importlib.util.spec_from_file_location("lyricsync_transcript", _MODULE_PATH)
transcript = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(transcript)


def _word(text, source, gs, ls=None, le=None, ge=None):
    """Build a merged-transcript word. local times default to global for simplicity."""
    return {
        "text": text,
        "source": source,
        "global_start": gs,
        "global_end": ge if ge is not None else gs + 0.4,
        "local_start": ls if ls is not None else gs,
        "local_end": le if le is not None else (ge if ge is not None else gs + 0.4),
    }


def _sentence(text, source, start, *, local_offset=0.0, word_dur=0.4, gap=0.1):
    """Build a run of words for a sentence with sequential global/local times."""
    words = []
    g = start
    for tok in text.split():
        words.append(_word(
            tok, source, g,
            ls=g - local_offset, le=g - local_offset + word_dur,
            ge=g + word_dur,
        ))
        g += word_dur + gap
    return words


# ── normalize_tokens ────────────────────────────────────────────────────────

def test_normalize_strips_punctuation_and_lowercases():
    assert transcript.normalize_tokens("Hello, World!") == ["hello", "world"]


def test_normalize_keeps_apostrophes_and_digits():
    assert transcript.normalize_tokens("It's 47.7 inches") == ["it's", "47", "7", "inches"]


def test_normalize_empty():
    assert transcript.normalize_tokens("") == []
    assert transcript.normalize_tokens("   !!! ") == []


# ── format_transcript ─────────────────────────────────────────────────────────

def test_format_groups_by_source_with_headers_and_no_timestamps():
    words = (
        _sentence("hello there friend", "a.mov", 0.0)
        + _sentence("second clip speaking", "b.mov", 100.0)
    )
    out = transcript.format_transcript(words)
    assert "=== a.mov ===" in out
    assert "=== b.mov ===" in out
    assert "hello there friend" in out
    # No timestamp markers leak into the prompt
    assert "LOCAL" not in out
    assert ":" not in out.replace("===", "")


def test_format_orders_sources_by_first_appearance():
    words = (
        _sentence("later clip", "z.mov", 500.0)
        + _sentence("early clip", "a.mov", 1.0)
    )
    out = transcript.format_transcript(words)
    assert out.index("=== a.mov ===") < out.index("=== z.mov ===")


def test_format_splits_paragraphs_on_long_gap():
    # Two sentences in the same source separated by a > 1.5s gap
    words = (
        _sentence("first thought here", "a.mov", 0.0)
        + _sentence("second thought here", "a.mov", 10.0)
    )
    out = transcript.format_transcript(words)
    section = out.split("=== a.mov ===")[1]
    assert "first thought here" in section
    assert "second thought here" in section
    # paragraph break (blank line) between them
    assert "\n\n" in section.strip()


# ── match_quote ───────────────────────────────────────────────────────────────

def test_match_exact_quote_returns_tight_span():
    words = _sentence("you know we love debris", "a.mov", 10.0)
    tokens, idx = transcript.build_token_index(words)
    m = transcript.match_quote("we love debris", words, tokens, idx)
    assert m is not None
    assert m["score"] == 1.0
    # span should start at "we" (3rd word) and end at "debris" (5th word)
    assert m["text"] == "we love debris"
    assert m["start"] == words[2]["local_start"]
    assert m["end"] == words[4]["local_end"]


def test_match_is_punctuation_and_case_insensitive():
    words = _sentence("this is the excess that we have", "a.mov", 0.0)
    tokens, idx = transcript.build_token_index(words)
    m = transcript.match_quote("THE EXCESS, that we...", words, tokens, idx)
    assert m is not None
    assert m["score"] == 1.0
    assert m["text"] == "the excess that we"


def test_match_tolerates_dropped_interior_word():
    words = _sentence("we are going to need a lot of foam", "a.mov", 0.0)
    tokens, idx = transcript.build_token_index(words)
    # Claude drops "going to" but anchors hold
    m = transcript.match_quote("we are need a lot of foam", words, tokens, idx)
    assert m is not None
    assert m["score"] >= 0.8
    # span anchored from "we" to "foam" — interior preserved from source
    assert m["text"].startswith("we are")
    assert m["text"].endswith("foam")


def test_match_no_overlap_returns_low_or_none():
    words = _sentence("completely unrelated speech here", "a.mov", 0.0)
    tokens, idx = transcript.build_token_index(words)
    m = transcript.match_quote("xyzzy plugh frobnicate", words, tokens, idx)
    assert m is None or m["score"] < transcript.MATCH_MIN_SCORE


def test_match_empty_quote_returns_none():
    words = _sentence("some words", "a.mov", 0.0)
    tokens, idx = transcript.build_token_index(words)
    assert transcript.match_quote("", words, tokens, idx) is None


# ── resolve_segments (fail-loud contract) ─────────────────────────────────────

def _index(words):
    return transcript.build_source_index(words)


def test_resolve_maps_quotes_across_sources():
    words = (
        _sentence("welcome to the home depot adventure", "a.mov", 0.0)
        + _sentence("now we are cutting the boards", "b.mov", 50.0)
    )
    index = _index(words)
    segments = [
        {"source": "a.mov", "quote": "the home depot adventure"},
        {"source": "b.mov", "quote": "cutting the boards"},
    ]
    ranges = transcript.resolve_segments(segments, index)
    assert len(ranges) == 2
    assert ranges[0]["source"] == "a.mov"
    assert ranges[0]["text"] == "the home depot adventure"
    assert ranges[1]["source"] == "b.mov"
    assert ranges[1]["text"] == "cutting the boards"
    # ranges carry numeric timestamps
    assert ranges[0]["end"] > ranges[0]["start"]


def test_resolve_unknown_source_fails_loud():
    words = _sentence("hello world", "a.mov", 0.0)
    index = _index(words)
    with pytest.raises(ValueError, match="unknown source"):
        transcript.resolve_segments(
            [{"source": "ghost.mov", "quote": "hello world"}], index
        )


def test_resolve_unmatchable_quote_fails_loud():
    words = _sentence("the quick brown fox", "a.mov", 0.0)
    index = _index(words)
    with pytest.raises(ValueError, match="could not match"):
        transcript.resolve_segments(
            [{"source": "a.mov", "quote": "totally different unrelated text here"}],
            index,
        )


def test_resolve_rounds_timestamps():
    words = _sentence("alpha beta gamma", "a.mov", 1.23456)
    index = _index(words)
    ranges = transcript.resolve_segments(
        [{"source": "a.mov", "quote": "alpha beta gamma"}], index
    )
    r = ranges[0]
    assert r["start"] == round(r["start"], 2)
    assert r["end"] == round(r["end"], 2)


# ── stories_as_text ───────────────────────────────────────────────────────────

def test_stories_as_text_shows_quotes_not_timestamps():
    stories = [{
        "title": "The Build",
        "description": "A focused cut.",
        "ranges": [
            {"source": "a.mov", "start": 1.0, "end": 4.0, "text": "we love debris"},
            {"source": "b.mov", "start": 5.0, "end": 9.0, "text": "cutting the boards"},
        ],
    }]
    out = transcript.stories_as_text(stories)
    assert "The Build" in out
    assert "we love debris" in out
    assert "[a.mov]" in out
    # no raw timestamp numbers from the ranges
    assert "1.0" not in out and "4.0" not in out


def test_stories_as_text_truncates_long_quotes():
    long_text = " ".join(["word"] * 60)
    stories = [{
        "title": "T", "description": "D",
        "ranges": [{"source": "a.mov", "start": 0, "end": 1, "text": long_text}],
    }]
    out = transcript.stories_as_text(stories)
    assert "…" in out


def test_stories_as_text_handles_missing_text():
    stories = [{
        "title": "T", "description": "D",
        "ranges": [{"source": "a.mov", "start": 0, "end": 1}],
    }]
    out = transcript.stories_as_text(stories)
    assert "(segment)" in out
