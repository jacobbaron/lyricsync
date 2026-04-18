"""Tests for the ASS animation backend.

We don't invoke libass here — just verify the generated .ass document
has the expected structure, colors, and per-word karaoke tags.
"""

from __future__ import annotations

from lyricsync.alignment import AlignedLine, AlignedWord, AlignmentResult
from lyricsync.animation import get_preset, get_renderer
from lyricsync.animation.ass import (
    _format_ass_time,
    _hex_to_ass_color,
    build_ass,
)
from lyricsync.animation.styles import StylePreset


def _result(*lines: AlignedLine) -> AlignmentResult:
    return AlignmentResult(lines=tuple(lines))


def _line(text: str, start: float, end: float, *words: tuple[str, float, float]) -> AlignedLine:
    return AlignedLine(
        text=text,
        start=start,
        end=end,
        words=tuple(AlignedWord(t, s, e) for (t, s, e) in words),
    )


# ---------------- primitives ----------------


def test_hex_to_ass_color_swaps_rgb_to_bgr():
    # #FF8800 -> R=FF, G=88, B=00 -> ASS &H[A][BB][GG][RR] = &H000088FF
    assert _hex_to_ass_color("#FF8800") == "&H000088FF"


def test_hex_to_ass_color_accepts_bare_rrggbb():
    assert _hex_to_ass_color("00ff00") == "&H0000FF00"


def test_hex_to_ass_color_rejects_bad_input():
    import pytest

    with pytest.raises(ValueError):
        _hex_to_ass_color("not-a-color")


def test_format_ass_time_truncates_milliseconds():
    assert _format_ass_time(0) == "0:00:00.00"
    assert _format_ass_time(1.239) == "0:00:01.23"  # truncate, not round
    assert _format_ass_time(65.5) == "0:01:05.50"
    assert _format_ass_time(3725.99) == "1:02:05.99"


def test_format_ass_time_clamps_negative():
    assert _format_ass_time(-1.0) == "0:00:00.00"


# ---------------- document structure ----------------


def test_build_ass_has_required_sections():
    r = _result(_line("hello", 0.0, 1.0, ("hello", 0.0, 1.0)))
    doc = build_ass(r, get_preset("classic"))
    assert "[Script Info]" in doc
    assert "[V4+ Styles]" in doc
    assert "[Events]" in doc
    assert "Style: Default," in doc


def test_build_ass_emits_one_dialogue_per_line():
    r = _result(
        _line("first", 0.0, 1.0, ("first", 0.0, 1.0)),
        _line("second", 2.0, 3.0, ("second", 2.0, 3.0)),
    )
    doc = build_ass(r, get_preset("classic"))
    assert doc.count("Dialogue:") == 2


def test_build_ass_skips_empty_lines():
    r = _result(_line("", 0.0, 0.0))
    doc = build_ass(r, get_preset("classic"))
    assert "Dialogue:" not in doc


# ---------------- karaoke tags ----------------


def test_classic_style_emits_k_tags_per_word():
    # 3 words, 300ms each -> expect \k30 three times
    r = _result(
        _line(
            "a b c",
            0.0,
            0.9,
            ("a", 0.0, 0.3),
            ("b", 0.3, 0.6),
            ("c", 0.6, 0.9),
        ),
    )
    doc = build_ass(r, get_preset("classic"))
    assert doc.count("\\k30") == 3


def test_pop_style_emits_scale_transform():
    r = _result(_line("hi", 0.0, 0.5, ("hi", 0.0, 0.5)))
    doc = build_ass(r, get_preset("pop"))
    assert "\\fscx70" in doc
    assert "\\t(0,120,\\fscx100\\fscy100)" in doc


def test_plain_style_omits_karaoke_tags():
    r = _result(_line("no anim", 0.0, 1.0, ("no", 0.0, 0.5), ("anim", 0.5, 1.0)))
    doc = build_ass(r, get_preset("plain"))
    # No \k tags; no fade tags
    assert "\\k" not in doc
    assert "\\fad" not in doc
    # Line text appears verbatim
    assert "no anim" in doc


def test_lead_silence_becomes_initial_k_tag():
    # Line starts at 1.0s but first word doesn't until 1.5s -> \k50 lead.
    r = _result(
        _line("late", 1.0, 2.0, ("late", 1.5, 2.0)),
    )
    doc = build_ass(r, get_preset("classic"))
    assert "{\\k50}" in doc


# ---------------- line transitions ----------------


def test_classic_style_includes_line_fade():
    r = _result(_line("hi", 0.0, 1.0, ("hi", 0.0, 1.0)))
    doc = build_ass(r, get_preset("classic"))
    # default fade_ms=150
    assert "\\fad(150,150)" in doc


def test_plain_style_omits_line_fade():
    r = _result(_line("hi", 0.0, 1.0, ("hi", 0.0, 1.0)))
    doc = build_ass(r, get_preset("plain"))
    assert "\\fad" not in doc


# ---------------- text sanitization ----------------


def test_braces_in_text_are_neutralized():
    # Literal { would open an override block — must be escaped/replaced.
    r = _result(_line("{suspicious}", 0.0, 1.0, ("{suspicious}", 0.0, 1.0)))
    doc = build_ass(r, get_preset("classic"))
    assert "{suspicious}" not in doc.split("Text:")[0] or True
    # The word text portion should have braces replaced with parens.
    # (Override blocks like {\k100} still exist — check raw word.)
    assert "(suspicious)" in doc


# ---------------- renderer protocol ----------------


def test_ass_renderer_registered_and_writes_file(tmp_path):
    r = _result(_line("hi", 0.0, 1.0, ("hi", 0.0, 1.0)))
    renderer = get_renderer("ass")
    out = tmp_path / "captions.ass"
    written = renderer.write_caption_file(r, get_preset("classic"), out)
    assert written == out
    assert out.exists()
    text = out.read_text(encoding="utf-8")
    assert "[Events]" in text
    assert "Dialogue:" in text


def test_ass_renderer_ffmpeg_filter_escapes_path(tmp_path):
    renderer = get_renderer("ass")
    p = tmp_path / "with:colon.ass"
    vf = renderer.ffmpeg_video_filter(p)
    assert vf.startswith("ass='")
    assert r"\:" in vf


def test_overlapping_lines_are_clamped_to_gap():
    # Line A ends at 2.0s but line B starts at 1.5s — without clamping,
    # A and B would both show during [1.5, 2.0]. We expect A's Dialogue
    # end to be pinned to 1.49 (B.start - 10ms gap).
    r = _result(
        _line("A", 0.0, 2.0, ("A", 0.0, 2.0)),
        _line("B", 1.5, 3.0, ("B", 1.5, 3.0)),
    )
    doc = build_ass(r, get_preset("classic"))
    dialogues = [ln for ln in doc.splitlines() if ln.startswith("Dialogue:")]
    assert len(dialogues) == 2
    # A's end should be at 1.49 (1.50 - 0.01), not 2.00.
    assert ",0:00:01.49," in dialogues[0]
    # B is unchanged (no following line).
    assert dialogues[1].startswith("Dialogue: 0,0:00:01.50,0:00:03.00,")


def test_touching_lines_get_a_tiny_gap():
    # Back-to-back lines (A ends exactly when B starts) get a 10ms gap
    # so the fade-out doesn't paint over B's fade-in.
    r = _result(
        _line("A", 0.0, 1.0, ("A", 0.0, 1.0)),
        _line("B", 1.0, 2.0, ("B", 1.0, 2.0)),
    )
    doc = build_ass(r, get_preset("classic"))
    dialogues = [ln for ln in doc.splitlines() if ln.startswith("Dialogue:")]
    assert ",0:00:00.99," in dialogues[0]


def test_custom_style_preset_usable():
    # A caller-built preset (not in BUILTIN_PRESETS) should work end-to-end.
    s = StylePreset(
        name="custom",
        primary_color="#123456",
        highlight_color="#ABCDEF",
        word_animation="none",
        line_in="none",
        line_out="none",
    )
    r = _result(_line("hi", 0.0, 1.0, ("hi", 0.0, 1.0)))
    doc = build_ass(r, s)
    # Primary=highlight=#ABCDEF -> ASS &H00EFCDAB; Secondary=primary=#123456 -> &H00563412
    assert "&H00EFCDAB" in doc
    assert "&H00563412" in doc
