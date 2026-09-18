"""Tests for lyric forced-alignment planning (modal/lyric_align.py).

Loads modal/lyric_align.py by path (modal/ is not an importable package and the
name collides with the pip `modal` package). The module is stdlib-only.
"""

import importlib.util
import sys
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "lyric_align.py"
_spec = importlib.util.spec_from_file_location("lyricsync_lyric_align", _MODULE_PATH)
la = importlib.util.module_from_spec(_spec)
# Register before exec: @dataclass resolves its own module from sys.modules.
sys.modules[_spec.name] = la
_spec.loader.exec_module(la)


def words(pairs):
    """[(text, start, end), ...] → the raw ASR word-row shape."""
    return [
        {"text": t, "local_start": s, "local_end": e} for t, s, e in pairs
    ]


# ---------------------------------------------------------------------------
# Parsing / normalization
# ---------------------------------------------------------------------------

def test_parse_drops_sections_and_blanks():
    raw = "[Verse 1]\nHold the line\n\nWalk the wire\n\n[Chorus]\nFade to grey\n"
    assert la.parse_lyrics_text(raw) == [
        "Hold the line",
        "Walk the wire",
        "Fade to grey",
    ]


def test_parse_keeps_line_verbatim():
    """Punctuation and capitalization survive — only matching is normalized."""
    assert la.parse_lyrics_text("Don't you go?\n") == ["Don't you go?"]


def test_parse_drops_punctuation_only_lines():
    assert la.parse_lyrics_text("---\nReal line\n***\n") == ["Real line"]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("Five", "five"),
        ("5", "five"),          # digits fold to number words, so "5" anchors "Five"
        ("FIVE,", "five"),
        ("don't", "don't"),
        ("—", ""),
    ],
)
def test_normalize_token(raw, expected):
    assert la.normalize_token(raw) == expected


# ---------------------------------------------------------------------------
# ASR hygiene
# ---------------------------------------------------------------------------

def test_clean_drops_repetition_loop():
    """A pile of words stamped at one instant is a Whisper loop, not evidence."""
    rows = words(
        [("hold", 1.0, 1.4), ("the", 1.4, 1.8)]
        + [("loop", 5.0, 5.0) for _ in range(6)]
        + [("line", 9.0, 9.4)]
    )
    cleaned = la.clean_asr_words(rows)
    assert [t for t, _, _ in cleaned] == ["hold", "the", "line"]


def test_clean_clamps_held_vowel():
    cleaned = la.clean_asr_words(words([("away", 10.0, 32.0)]))
    assert cleaned == [("away", 10.0, 10.0 + la.MAX_ANCHOR_WORD_S)]


def test_clean_drops_backwards_time():
    cleaned = la.clean_asr_words(
        words([("one", 5.0, 5.5), ("two", 1.0, 1.5), ("three", 6.0, 6.5)])
    )
    assert [t for t, _, _ in cleaned] == ["one", "three"]


# ---------------------------------------------------------------------------
# Anchoring
# ---------------------------------------------------------------------------

def test_anchor_requires_a_run():
    """A lone coincidental token match must not anchor a line."""
    lines = ["alpha bravo charlie", "delta echo foxtrot"]
    asr = words([("zulu", 1.0, 1.5), ("delta", 2.0, 2.5), ("yankee", 3.0, 3.5)])
    assert la.line_anchors(lines, asr) == {}


def test_anchor_from_matching_run_survives_mishearing():
    """The point of the whole exercise: a misheard word between two correct
    ones still leaves the line anchored."""
    lines = ["hold the line tonight"]
    asr = words(
        [("hold", 4.0, 4.4), ("the", 4.4, 4.8), ("lion", 4.8, 5.2), ("tonight", 5.2, 6.0)]
    )
    anchors = la.line_anchors(lines, asr)
    assert 0 in anchors
    assert anchors[0][0] == pytest.approx(4.0)


# ---------------------------------------------------------------------------
# Window planning
# ---------------------------------------------------------------------------

def test_anchored_lines_get_their_own_padded_window():
    lines = ["hold the line", "walk the wire"]
    asr = words(
        [
            ("hold", 10.0, 10.4), ("the", 10.4, 10.8), ("line", 10.8, 11.6),
            ("walk", 20.0, 20.4), ("the", 20.4, 20.8), ("wire", 20.8, 21.6),
        ]
    )
    wins = la.plan_windows(lines, asr, duration=40.0)
    assert len(wins) == 2
    assert all(w.anchored for w in wins)
    assert wins[0].start == pytest.approx(10.0 - la.PAD_HEAD_S)
    assert wins[0].lines == ("hold the line",)


def test_windows_never_overlap_and_stay_in_bounds():
    lines = ["hold the line", "walk the wire"]
    asr = words(
        [
            ("hold", 0.1, 0.4), ("the", 0.4, 0.8), ("line", 0.8, 1.6),
            ("walk", 1.9, 2.2), ("the", 2.2, 2.6), ("wire", 2.6, 3.0),
        ]
    )
    wins = la.plan_windows(lines, asr, duration=4.0)
    assert wins[0].start >= 0.0
    assert wins[-1].end <= 4.0
    for a, b in zip(wins, wins[1:]):
        assert b.start >= a.end


def test_unanchored_run_spans_the_gap_between_anchors():
    """Lines the ASR missed wholesale get the leftover audio between anchors."""
    lines = ["hold the line", "missing one", "missing two", "walk the wire"]
    asr = words(
        [
            ("hold", 5.0, 5.4), ("the", 5.4, 5.8), ("line", 5.8, 6.4),
            ("walk", 30.0, 30.4), ("the", 30.4, 30.8), ("wire", 30.8, 31.4),
        ]
    )
    wins = la.plan_windows(lines, asr, duration=40.0)
    gap = [w for w in wins if not w.anchored]
    assert len(gap) == 1
    assert gap[0].lines == ("missing one", "missing two")
    assert gap[0].start == pytest.approx(6.4 + la.PAD_TAIL_S)
    assert gap[0].end == pytest.approx(30.0 - la.PAD_HEAD_S)


def test_repeated_refrain_shares_one_window():
    """Identical adjacent lines can't be told apart by the matcher, so they
    must not be anchored individually — they share the section's audio."""
    lines = ["verse line here", "fade away", "fade away", "fade away", "last line here"]
    asr = words(
        [
            ("verse", 5.0, 5.4), ("line", 5.4, 5.8), ("here", 5.8, 6.2),
            # the ASR only caught one of the three repeats
            ("fade", 8.0, 8.4), ("away", 8.4, 9.0),
            ("last", 20.0, 20.4), ("line", 20.4, 20.8), ("here", 20.8, 21.2),
        ]
    )
    wins = la.plan_windows(lines, asr, duration=30.0)
    refrain = [w for w in wins if w.lines and w.lines[0] == "fade away"]
    assert len(refrain) == 1
    assert refrain[0].lines == ("fade away",) * 3
    assert not refrain[0].anchored


def test_starved_window_is_merged_into_a_neighbour():
    """Anchors that cluster too tightly for the text would starve the aligner;
    the window is merged rather than left sub-second."""
    lines = ["a much longer line than the audio allows", "second line here"]
    asr = words(
        [
            ("a", 10.0, 10.02), ("much", 10.02, 10.05),
            ("second", 12.0, 13.0), ("line", 13.0, 14.0), ("here", 14.0, 18.0),
        ]
    )
    wins = la.plan_windows(lines, asr, duration=30.0)
    assert len(wins) == 1
    assert wins[0].lines == tuple(lines)


def test_empty_lyrics_plan_is_empty():
    assert la.plan_windows([], words([]), duration=10.0) == []


def test_no_asr_yields_one_whole_track_window():
    lines = ["hold the line", "walk the wire"]
    wins = la.plan_windows(lines, [], duration=12.0)
    assert len(wins) == 1
    assert wins[0].lines == tuple(lines)
    assert (wins[0].start, wins[0].end) == (0.0, 12.0)


def test_window_as_segment_shape():
    seg = la.Window(("hold the line",), 1.0, 2.0, True).as_segment()
    assert seg == {"text": "hold the line", "start": 1.0, "end": 2.0}


# ---------------------------------------------------------------------------
# Aggregation / tidy
# ---------------------------------------------------------------------------

def aligned(*triples):
    return [{"word": w, "start": s, "end": e, "score": c} for w, s, e, c in triples]


def test_aggregate_folds_words_into_lines():
    lines = ["hold the line", "walk on"]
    rows = la.aggregate_words(
        lines,
        aligned(
            ("hold", 1.0, 1.4, 0.8), ("the", 1.4, 1.8, 0.6), ("line", 1.8, 2.5, 0.7),
            ("walk", 3.0, 3.4, 0.9), ("on", 3.4, 3.9, 0.5),
        ),
    )
    assert [r["text"] for r in rows] == lines
    assert (rows[0]["start"], rows[0]["end"]) == (1.0, 2.5)
    assert rows[0]["score"] == pytest.approx(0.7, abs=0.01)
    assert (rows[1]["start"], rows[1]["end"]) == (3.0, 3.9)


def test_aggregate_line_with_no_timed_words_is_untimed_not_invented():
    rows = la.aggregate_words(
        ["hold the line"],
        [{"word": "hold"}, {"word": "the"}, {"word": "line"}],
    )
    assert rows[0]["start"] is None and rows[0]["end"] is None


def test_aggregate_ignores_punctuation_only_tokens_in_the_count():
    """Token counting must match what the aligner emits, which skips tokens
    with no letters — otherwise every later line is off by one."""
    rows = la.aggregate_words(
        ["hold — line", "walk on"],
        aligned(
            ("hold", 1.0, 1.4, 0.8), ("line", 1.4, 2.0, 0.7),
            ("walk", 3.0, 3.4, 0.9), ("on", 3.4, 3.9, 0.5),
        ),
    )
    assert (rows[1]["start"], rows[1]["end"]) == (3.0, 3.9)


def test_tidy_drops_untimed_and_enforces_order():
    rows = [
        {"text": "a", "start": 1.0, "end": 3.0, "score": 0.5},
        {"text": "b", "start": None, "end": None, "score": 0.0},
        {"text": "c", "start": 2.0, "end": 4.0, "score": 0.5},
    ]
    out = la.tidy_lines(rows)
    assert [r["text"] for r in out] == ["a", "c"]
    assert out[1]["start"] >= out[0]["end"]


def test_tidy_caps_a_held_final_syllable():
    rows = [{"text": "away", "start": 10.0, "end": 60.0, "score": 0.5}]
    out = la.tidy_lines(rows, max_hold=6.0)
    assert out[0]["end"] == pytest.approx(16.0)
