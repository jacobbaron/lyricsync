"""Tests for libass captions (modal/captions.py) and their timeline wiring."""

import importlib.util
import re
from pathlib import Path

import pytest

_MODAL = Path(__file__).resolve().parent.parent / "modal"


def _load(name, file):
    spec = importlib.util.spec_from_file_location(name, _MODAL / file)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


cap = _load("lyricsync_captions", "captions.py")
tl = _load("lyricsync_timeline_caps", "timeline.py")
FONTS_DIR = str(_MODAL / "assets" / "fonts")

LINE = "Stare at my reflection in the glass"


def _words(text=LINE, step=0.4):
    return [
        {"text": w, "start": round(i * step, 3), "end": round((i + 1) * step, 3)}
        for i, w in enumerate(text.split())
    ]


def _item(**kw):
    item = {"id": "t1", "text": LINE, "start": 10.0, "end": 14.0,
            "font": "instrument-serif", "anim": "highlight", "size": 54,
            "position": "lower", "fade": 0.2, "words": _words()}
    item.update(kw)
    return item


def _events(ass):
    return [l for l in ass.splitlines() if l.startswith("Dialogue:")]


def _pos(event):
    return tuple(map(float, re.search(r"\\pos\(([\d.]+),([\d.]+)\)", event).groups()))


def _text(event):
    return event.rsplit("}", 1)[1]


def test_registries_agree():
    assert set(tl.TEXT_FONTS) == set(cap.FONTS)
    assert tuple(tl.TEXT_ANIMS) == cap.ANIM_STYLES


def test_every_font_has_metrics_and_file():
    metrics = cap._metrics(FONTS_DIR)
    for key, (_, file, _) in cap.FONTS.items():
        assert file in metrics, key
        assert (Path(FONTS_DIR) / file).exists(), key


def test_break_rows_balances_instead_of_orphaning():
    # Greedy would give [a b c][d]; balanced gives two even rows.
    rows = cap.break_rows([100, 100, 100, 100], 10, 330)
    assert rows == [0, 0, 1, 1]


def test_break_rows_single_row_when_it_fits():
    assert cap.break_rows([50, 50, 50], 5, 1000) == [0, 0, 0]


@pytest.mark.parametrize("anim", ["line", "highlight", "pop"])
def test_word_positions_never_move(anim):
    """Each word sits at one fixed position for the whole caption, so a
    highlighted/scaled word can never re-flow the line."""
    ass = cap.build_ass([_item(anim=anim)], 1080, 1920, FONTS_DIR)
    by_word = {}
    for ev in _events(ass):
        by_word.setdefault(_text(ev), set()).add(_pos(ev))
    assert set(by_word) == set(LINE.split())
    assert all(len(p) == 1 for p in by_word.values())


def test_rows_fit_the_frame():
    long = " ".join([LINE] * 3)
    ass = cap.build_ass([_item(text=long, words=_words(long, 0.1), anim="line")],
                        1080, 1920, FONTS_DIR)
    ys = {_pos(e)[1] for e in _events(ass)}
    assert len(ys) >= 2  # wrapped


def test_highlight_tints_the_sung_word_only_while_sung():
    ass = cap.build_ass([_item()], 1080, 1920, FONTS_DIR)
    hi = [e for e in _events(ass) if "\\i1" in e]
    assert len(hi) == len(LINE.split())
    first = hi[0]
    assert first.split(",")[1] == "0:00:10.00"  # "Stare" sung at item start
    assert "&H0060C8F5&" in first  # default gold, BGR


def test_pop_hides_words_until_sung():
    ass = cap.build_ass([_item(anim="pop")], 1080, 1920, FONTS_DIR)
    glass = [e for e in _events(ass) if _text(e) == "glass"]
    # Starts at 10 + 6*0.4 = 12.4, never before.
    assert min(e.split(",")[1] for e in glass) == "0:00:12.40"


def test_word_style_shows_one_word_at_a_time():
    ass = cap.build_ass([_item(anim="word")], 1080, 1920, FONTS_DIR)
    evs = _events(ass)
    assert [_text(e) for e in evs] == LINE.split()
    assert len({_pos(e) for e in evs}) == 1


def test_missing_words_falls_back_to_line():
    ass = cap.build_ass([_item(words=None)], 1080, 1920, FONTS_DIR)
    assert len(_events(ass)) == len(LINE.split())
    assert not any("\\i1" in e for e in _events(ass))


def test_braces_cannot_inject_tags():
    ass = cap.build_ass([_item(text="hi {\\b1} there", words=None, anim="line")],
                        1080, 1920, FONTS_DIR)
    assert "\\b1" not in ass


def test_validation_accepts_and_rejects():
    base = {"version": 1, "tracks": [
        {"type": "video", "items": [{"id": "v1", "kind": "blank", "duration": 5}]},
        {"type": "text", "items": [_item(start=0.0, end=4.0)]},
    ]}
    assert tl.validate_timeline(base) == []
    for bad in ({"font": "comic-sans"}, {"anim": "karaoke"},
                {"highlight_color": "puce"},
                {"words": [{"text": "x", "start": 2, "end": 1}]},
                {"words": "nope"}):
        t = {**base, "tracks": [base["tracks"][0],
             {"type": "text", "items": [_item(start=0.0, end=4.0, **bad)]}]}
        assert tl.validate_timeline(t), bad


def test_compile_routes_styled_items_to_ass(tmp_path):
    timeline = {"version": 1, "width": 1080, "height": 1920, "tracks": [
        {"type": "video", "items": [{"id": "v1", "kind": "blank", "duration": 5}]},
        {"type": "text", "items": [
            _item(start=0.0, end=4.0),
            {"id": "t2", "text": "Title", "start": 0.0, "end": 2.0},
        ]},
    ]}
    out = tl.compile_timeline(timeline, lambda i: "x.mp4", str(tmp_path),
                              "/f/overlay.ttf", fonts_dir=FONTS_DIR)
    fc = out["filter_complex"]
    assert fc.count("drawtext=") == 1  # the plain title card
    assert f"ass=filename={tmp_path}/captions.ass:fontsdir={FONTS_DIR}" in fc
    assert any(p.endswith("captions.ass") for p, _ in out["text_files"])


def test_add_text_op_carries_caption_keys():
    t = {"version": 1, "tracks": [
        {"type": "video", "items": [{"id": "v1", "kind": "blank", "duration": 5}]}]}
    out = tl.apply_ops(t, [{"op": "add_text", "text": LINE, "start": 0, "end": 3,
                                "font": "playfair", "anim": "pop",
                                "words": _words()}])
    item = tl.text_items(out)[0]
    assert item["font"] == "playfair" and item["anim"] == "pop"
    assert len(item["words"]) == 7
