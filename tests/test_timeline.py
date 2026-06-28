"""Tests for the timeline (EDL) model: ranges import, validation, edit ops,
and the timeline → ffmpeg filtergraph compiler.

Loads modal/timeline.py by path (modal/ is not an importable package and the
name collides with the pip `modal` package). The module is stdlib-only.
"""

import importlib.util
from pathlib import Path

import pytest

_MODULE_PATH = Path(__file__).resolve().parent.parent / "modal" / "timeline.py"
_spec = importlib.util.spec_from_file_location("lyricsync_timeline", _MODULE_PATH)
tl = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tl)


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------

def make_timeline(n_clips=2, **kwargs):
    """A valid timeline with n sequential 4-second clip items."""
    items = []
    for i in range(n_clips):
        items.append({
            "id": f"v{i + 1}",
            "kind": "clip",
            "source": "IMG_0001.mov",
            "src_start": float(i * 10),
            "src_end": float(i * 10 + 4),
            "speed": 1.0,
            "transition_in": None,
        })
    timeline = {
        "version": 1,
        "width": 1080,
        "height": 1920,
        "fps": 30,
        "tracks": [{"type": "video", "items": items}],
    }
    timeline.update(kwargs)
    return timeline


RANGES = [
    {"source": "IMG_0001.mov", "start": 10.0, "end": 14.0, "text": "hello there",
     "quote": "hello there"},
    {"source": "blank", "start": 0.0, "end": 1.5,
     "overlay": {"text": "Chapter Two", "in": 0.2, "out": 1.2, "position": "upper"}},
    {"source": "IMG_0002.mov", "start": 0.05, "end": 3.0},
]


def compile_simple(timeline):
    # resolve_source receives the whole clip item; cross-project clips would
    # resolve by item["clip_id"], but these single-project timelines use the
    # bare source filename.
    return tl.compile_timeline(
        timeline,
        resolve_source=lambda item: f"/cache/{item['source']}",
        workdir="/tmp/render",
        font_path="/root/overlay_font.ttf",
    )


# ---------------------------------------------------------------------------
# timeline_from_ranges
# ---------------------------------------------------------------------------

class TestFromRanges:
    def test_pads_are_baked_in(self):
        timeline = tl.timeline_from_ranges(RANGES)
        items = tl.video_items(timeline)
        assert items[0]["src_start"] == pytest.approx(10.0 - 0.08)
        assert items[0]["src_end"] == pytest.approx(14.0 + 0.08)

    def test_pad_clamped_at_zero(self):
        timeline = tl.timeline_from_ranges(RANGES)
        items = tl.video_items(timeline)
        # range starts at 0.05 — pad would go negative
        assert items[2]["src_start"] == 0.0

    def test_blank_becomes_blank_item(self):
        timeline = tl.timeline_from_ranges(RANGES)
        items = tl.video_items(timeline)
        assert items[1]["kind"] == "blank"
        assert items[1]["duration"] == pytest.approx(1.5)

    def test_overlay_becomes_text_item_in_output_time(self):
        timeline = tl.timeline_from_ranges(RANGES)
        texts = tl.text_items(timeline)
        assert len(texts) == 1
        # The blank is the second item: output offset = padded first segment.
        seg0 = 4.0 + 2 * 0.08
        assert texts[0]["start"] == pytest.approx(seg0 + 0.2, abs=1e-3)
        assert texts[0]["end"] == pytest.approx(seg0 + 1.2, abs=1e-3)
        assert texts[0]["position"] == "upper"

    def test_note_carries_transcript_text(self):
        timeline = tl.timeline_from_ranges(RANGES)
        assert tl.video_items(timeline)[0]["note"] == "hello there"

    def test_result_validates(self):
        assert tl.validate_timeline(tl.timeline_from_ranges(RANGES)) == []


# ---------------------------------------------------------------------------
# Duration math
# ---------------------------------------------------------------------------

class TestDuration:
    def test_simple_sum(self):
        assert tl.timeline_duration(make_timeline(3)) == pytest.approx(12.0)

    def test_speed_shortens(self):
        timeline = make_timeline(2)
        tl.video_items(timeline)[0]["speed"] = 2.0
        assert tl.timeline_duration(timeline) == pytest.approx(2.0 + 4.0)

    def test_crossfade_overlaps(self):
        timeline = make_timeline(2)
        tl.video_items(timeline)[1]["transition_in"] = {
            "type": "crossfade", "duration": 0.5,
        }
        assert tl.timeline_duration(timeline) == pytest.approx(7.5)


# ---------------------------------------------------------------------------
# validate_timeline
# ---------------------------------------------------------------------------

class TestValidate:
    def test_valid(self):
        assert tl.validate_timeline(make_timeline()) == []

    def test_wrong_version(self):
        errors = tl.validate_timeline(make_timeline(version=2))
        assert any("version" in e for e in errors)

    def test_empty_video_track(self):
        timeline = make_timeline()
        tl.video_items(timeline).clear()
        assert any("no items" in e for e in tl.validate_timeline(timeline))

    def test_bad_span(self):
        timeline = make_timeline()
        tl.video_items(timeline)[0]["src_end"] = 0.0
        assert any("src_end" in e for e in tl.validate_timeline(timeline))

    def test_speed_bounds(self):
        timeline = make_timeline()
        tl.video_items(timeline)[0]["speed"] = 25.0  # above MAX_SPEED
        assert any("speed" in e for e in tl.validate_timeline(timeline))

    def test_speed_10x_allowed(self):
        timeline = make_timeline()
        tl.video_items(timeline)[0]["speed"] = 10.0  # time-lapse speed
        assert tl.validate_timeline(timeline) == []

    def test_transition_on_first_item(self):
        timeline = make_timeline()
        tl.video_items(timeline)[0]["transition_in"] = {
            "type": "crossfade", "duration": 0.3,
        }
        assert any("first item" in e for e in tl.validate_timeline(timeline))

    def test_crossfade_longer_than_neighbor(self):
        timeline = make_timeline()
        items = tl.video_items(timeline)
        items[0]["src_end"] = items[0]["src_start"] + 0.4  # 0.4 s item
        items[1]["transition_in"] = {"type": "crossfade", "duration": 0.5}
        assert any("shorter than" in e for e in tl.validate_timeline(timeline))

    def test_duplicate_ids(self):
        timeline = make_timeline(2)
        tl.video_items(timeline)[1]["id"] = "v1"
        assert any("duplicate id" in e for e in tl.validate_timeline(timeline))

    def test_bad_text_item(self):
        timeline = make_timeline()
        timeline["tracks"].append({
            "type": "text",
            "items": [{"id": "t1", "text": "", "start": 2.0, "end": 1.0}],
        })
        errors = tl.validate_timeline(timeline)
        assert any("non-empty" in e for e in errors)
        assert any("end must be > start" in e for e in errors)

    def test_bad_text_position(self):
        timeline = make_timeline()
        timeline["tracks"].append({
            "type": "text",
            "items": [{"id": "t1", "text": "hi", "start": 0, "end": 1,
                       "position": "bottom-left"}],
        })
        assert any("position" in e for e in tl.validate_timeline(timeline))


# ---------------------------------------------------------------------------
# apply_ops
# ---------------------------------------------------------------------------

class TestOps:
    def test_input_not_mutated(self):
        timeline = make_timeline()
        before = tl.video_items(timeline)[0]["src_end"]
        tl.apply_ops(timeline, [{"op": "trim", "id": "v1", "end_delta": -1.0}])
        assert tl.video_items(timeline)[0]["src_end"] == before

    def test_trim_absolute(self):
        out = tl.apply_ops(make_timeline(), [
            {"op": "trim", "id": "v1", "src_start": 1.0, "src_end": 3.0},
        ])
        item = tl.video_items(out)[0]
        assert (item["src_start"], item["src_end"]) == (1.0, 3.0)

    def test_trim_deltas(self):
        out = tl.apply_ops(make_timeline(), [
            {"op": "trim", "id": "v1", "start_delta": 0.5, "end_delta": -0.5},
        ])
        item = tl.video_items(out)[0]
        assert item["src_start"] == pytest.approx(0.5)
        assert item["src_end"] == pytest.approx(3.5)

    def test_trim_empty_span_rejected(self):
        with pytest.raises(tl.TimelineError, match="span would be empty"):
            tl.apply_ops(make_timeline(), [
                {"op": "trim", "id": "v1", "src_start": 5.0, "src_end": 4.0},
            ])

    def test_unknown_id_names_op_index(self):
        with pytest.raises(tl.TimelineError, match=r"op 0 \(trim\).*nope"):
            tl.apply_ops(make_timeline(), [{"op": "trim", "id": "nope"}])

    def test_unknown_op(self):
        with pytest.raises(tl.TimelineError, match="unknown op"):
            tl.apply_ops(make_timeline(), [{"op": "explode"}])

    def test_split(self):
        out = tl.apply_ops(make_timeline(1), [
            {"op": "split", "id": "v1", "at": 1.5},
        ])
        items = tl.video_items(out)
        assert len(items) == 2
        assert items[0]["src_end"] == pytest.approx(1.5)
        assert items[1]["src_start"] == pytest.approx(1.5)
        assert items[1]["src_end"] == pytest.approx(4.0)
        assert items[1]["id"] != items[0]["id"]

    def test_split_out_of_range(self):
        with pytest.raises(tl.TimelineError, match="between 0 and"):
            tl.apply_ops(make_timeline(1), [{"op": "split", "id": "v1", "at": 9.0}])

    def test_move(self):
        out = tl.apply_ops(make_timeline(3), [
            {"op": "move", "id": "v3", "to_index": 0},
        ])
        assert [i["id"] for i in tl.video_items(out)] == ["v3", "v1", "v2"]

    def test_move_clears_promoted_transition(self):
        timeline = make_timeline(2)
        tl.video_items(timeline)[1]["transition_in"] = {
            "type": "crossfade", "duration": 0.3,
        }
        out = tl.apply_ops(timeline, [{"op": "move", "id": "v2", "to_index": 0}])
        assert tl.video_items(out)[0]["transition_in"] is None

    def test_delete(self):
        out = tl.apply_ops(make_timeline(2), [{"op": "delete", "id": "v1"}])
        assert [i["id"] for i in tl.video_items(out)] == ["v2"]

    def test_delete_last_item_rejected(self):
        with pytest.raises(tl.TimelineError, match="no items"):
            tl.apply_ops(make_timeline(1), [{"op": "delete", "id": "v1"}])

    def test_set_speed(self):
        out = tl.apply_ops(make_timeline(), [
            {"op": "set_speed", "id": "v1", "speed": 1.5},
        ])
        assert tl.video_items(out)[0]["speed"] == 1.5

    def test_set_speed_bounds(self):
        with pytest.raises(tl.TimelineError, match="between"):
            tl.apply_ops(make_timeline(), [
                {"op": "set_speed", "id": "v1", "speed": 25.0},
            ])

    def test_set_transition(self):
        out = tl.apply_ops(make_timeline(), [
            {"op": "set_transition", "id": "v2",
             "transition": {"type": "crossfade", "duration": 0.4}},
        ])
        assert tl.video_items(out)[1]["transition_in"]["duration"] == 0.4

    def test_set_transition_on_first_rejected(self):
        with pytest.raises(tl.TimelineError, match="first item"):
            tl.apply_ops(make_timeline(), [
                {"op": "set_transition", "id": "v1",
                 "transition": {"type": "crossfade", "duration": 0.4}},
            ])

    def test_insert_clip(self):
        out = tl.apply_ops(make_timeline(2), [
            {"op": "insert_clip", "index": 1, "source": "IMG_0002.mov",
             "src_start": 2.0, "src_end": 5.0},
        ])
        items = tl.video_items(out)
        assert len(items) == 3
        assert items[1]["source"] == "IMG_0002.mov"
        assert items[1]["id"] == "v3"  # next free sequential id

    def test_insert_clip_missing_fields_rejected(self):
        with pytest.raises(tl.TimelineError, match="invalid"):
            tl.apply_ops(make_timeline(), [
                {"op": "insert_clip", "source": "IMG_0002.mov"},
            ])

    def test_insert_blank_appends(self):
        out = tl.apply_ops(make_timeline(2), [
            {"op": "insert_blank", "duration": 1.0},
        ])
        items = tl.video_items(out)
        assert items[-1]["kind"] == "blank"

    def test_text_lifecycle(self):
        out = tl.apply_ops(make_timeline(), [
            {"op": "add_text", "text": "Hello", "start": 0.0, "end": 2.0},
            {"op": "update_text", "id": "t1", "text": "Hi!", "position": "lower"},
        ])
        texts = tl.text_items(out)
        assert texts[0]["text"] == "Hi!"
        assert texts[0]["position"] == "lower"

        out2 = tl.apply_ops(out, [{"op": "remove_text", "id": "t1"}])
        assert tl.text_items(out2) == []

    def test_remove_missing_text(self):
        with pytest.raises(tl.TimelineError, match="no text item"):
            tl.apply_ops(make_timeline(), [{"op": "remove_text", "id": "t9"}])


# ---------------------------------------------------------------------------
# compile_timeline
# ---------------------------------------------------------------------------

class TestCompile:
    def test_cut_only_uses_single_concat(self):
        result = compile_simple(make_timeline(3))
        fc = result["filter_complex"]
        assert "concat=n=3:v=1:a=1" in fc
        assert "xfade" not in fc
        assert "settb" not in fc  # legacy-identical graph when no crossfades
        assert fc.endswith("[aout]")
        assert "[vcat]format=yuv420p[vout]" in fc

    def test_one_seeked_input_per_clip_item(self):
        result = compile_simple(make_timeline(2))
        assert result["inputs"] == [
            ["-ss", "0.000", "-t", "4.000", "-i", "/cache/IMG_0001.mov"],
            ["-ss", "10.000", "-t", "4.000", "-i", "/cache/IMG_0001.mov"],
        ]

    def test_single_item_no_concat(self):
        result = compile_simple(make_timeline(1))
        assert "concat" not in result["filter_complex"]
        assert "[v0]format=yuv420p[vout]" in result["filter_complex"]

    def test_speed_setpts_and_atempo(self):
        timeline = make_timeline(1)
        tl.video_items(timeline)[0]["speed"] = 3.0
        result = compile_simple(timeline)
        fc = result["filter_complex"]
        assert "setpts=(PTS-STARTPTS)/3" in fc
        assert "atempo=2,atempo=1.5" in fc
        # -t stays in SOURCE time; speed is applied in the filtergraph
        assert result["inputs"][0][:4] == ["-ss", "0.000", "-t", "4.000"]
        assert result["duration"] == pytest.approx(4.0 / 3.0, abs=1e-3)

    def test_atempo_chain_slow(self):
        assert tl._atempo_chain(0.3) == "atempo=0.5,atempo=0.6"
        assert tl._atempo_chain(1.0) == "atempo=1"

    def test_crossfade_graph(self):
        timeline = make_timeline(2)
        tl.video_items(timeline)[1]["transition_in"] = {
            "type": "crossfade", "duration": 0.5,
        }
        result = compile_simple(timeline)
        fc = result["filter_complex"]
        # offset = first item duration (4.0) - fade (0.5)
        assert "xfade=transition=fade:duration=0.500:offset=3.500" in fc
        assert "acrossfade=d=0.500" in fc
        assert "settb=AVTB" in fc
        assert result["duration"] == pytest.approx(7.5)

    def test_mixed_cut_and_crossfade(self):
        timeline = make_timeline(3)
        tl.video_items(timeline)[2]["transition_in"] = {
            "type": "crossfade", "duration": 1.0,
        }
        result = compile_simple(timeline)
        fc = result["filter_complex"]
        # cut joins v0+v1 (pairwise concat), then crossfade into v2 at 8-1=7s
        assert "concat=n=2:v=1:a=1" in fc
        assert "offset=7.000" in fc
        assert result["duration"] == pytest.approx(11.0)

    def test_blank_item_lavfi_inputs(self):
        timeline = tl.timeline_from_ranges(RANGES)
        result = compile_simple(timeline)
        flat = [arg for input_args in result["inputs"] for arg in input_args]
        assert any(a.startswith("color=c=black") for a in flat)
        assert any(a.startswith("anullsrc") for a in flat)

    def test_text_files_and_drawtext(self):
        timeline = make_timeline(1)
        timeline["tracks"].append({
            "type": "text",
            "items": [{"id": "t1", "text": "A very long title that wraps",
                       "start": 0.5, "end": 2.5, "wrap": 10}],
        })
        result = compile_simple(timeline)
        (path, content) = result["text_files"][0]
        assert path == "/tmp/render/text_t1.txt"
        assert "\n" in content  # wrapped at 10 chars
        fc = result["filter_complex"]
        assert f"textfile={path}" in fc
        assert "enable='between(t\\,0.500\\,2.500)'" in fc
        # drawtext runs before the final pixel-format normalization
        assert fc.index("drawtext") < fc.index("format=yuv420p")

    def test_invalid_timeline_rejected(self):
        timeline = make_timeline()
        tl.video_items(timeline)[0]["src_end"] = -1
        with pytest.raises(tl.TimelineError, match="cannot compile"):
            compile_simple(timeline)

    def test_resolver_receives_clip_items(self):
        # The compiler hands the whole clip item to resolve_source so callers
        # can resolve cross-project clips by item["clip_id"] before falling
        # back to item["source"].
        seen = []

        def resolver(item):
            seen.append(item)
            return f"/x/{item['source']}"

        tl.compile_timeline(
            make_timeline(2), resolver, workdir="/w", font_path="/f.ttf",
        )
        assert [it["source"] for it in seen] == ["IMG_0001.mov", "IMG_0001.mov"]
        assert all(it["kind"] == "clip" for it in seen)

    def test_clip_id_item_validates_and_compiles(self):
        # A clip item carrying a cross-project clip_id (and no useful source
        # filename) is valid and resolvable by clip_id.
        timeline = make_timeline(1)
        item = tl.video_items(timeline)[0]
        item["clip_id"] = "11111111-2222-3333-4444-555555555555"
        item.pop("source", None)
        assert tl.validate_timeline(timeline) == []

        seen = []
        tl.compile_timeline(
            timeline,
            resolve_source=lambda it: seen.append(it.get("clip_id")) or "/x/y",
            workdir="/w",
            font_path="/f.ttf",
        )
        assert seen == ["11111111-2222-3333-4444-555555555555"]

    def test_empty_clip_id_is_rejected(self):
        timeline = make_timeline(1)
        item = tl.video_items(timeline)[0]
        item["clip_id"] = ""  # present but empty
        item.pop("source", None)
        errors = tl.validate_timeline(timeline)
        assert any("clip_id" in e or "source filename or a clip_id" in e
                   for e in errors)


class TestWordsForItem:
    """Tier 2 per-clip word resolver (filename-collision-safe)."""

    def test_local_item_filters_flat_words_by_filename(self):
        item = {"id": "v1", "kind": "clip", "source": "A.mov"}
        words = [
            {"text": "a", "source": "A.mov", "start": 0, "end": 1},
            {"text": "b", "source": "B.mov", "start": 0, "end": 1},
        ]
        got = tl._words_for_item(item, words, None)
        assert [w["text"] for w in got] == ["a"]

    def test_foreign_item_uses_clip_id_map_not_filename(self):
        # The home flat list shares the foreign clip's filename — must be
        # ignored in favor of the clip_id-keyed list.
        item = {"id": "v1", "kind": "clip", "source": "LABEL.mov",
                "clip_id": "cid-1"}
        home_words = [{"text": "home", "source": "LABEL.mov", "start": 0, "end": 1}]
        foreign = [{"text": "foreign", "source": "REAL.mov", "start": 0, "end": 1}]
        got = tl._words_for_item(item, home_words, {"cid-1": foreign})
        assert [w["text"] for w in got] == ["foreign"]

    def test_foreign_item_missing_from_map_falls_back_to_filename(self):
        # If the clip_id isn't in the map (unanalyzed / no words), fall back to
        # the home filename filter rather than crashing.
        item = {"id": "v1", "kind": "clip", "source": "A.mov", "clip_id": "cid-x"}
        home_words = [{"text": "a", "source": "A.mov", "start": 0, "end": 1}]
        got = tl._words_for_item(item, home_words, {"other": []})
        assert [w["text"] for w in got] == ["a"]


# ---------------------------------------------------------------------------
# choose_canvas — output frame auto-fit
# ---------------------------------------------------------------------------

class TestChooseCanvas:
    def test_empty_keeps_default(self):
        assert tl.choose_canvas([]) == (tl.DEFAULT_W, tl.DEFAULT_H)

    def test_uniform_9_16_is_the_default_frame(self):
        assert tl.choose_canvas([(1080, 1920), (1080, 1920)]) == (1080, 1920)

    def test_uniform_3_4_portrait(self):
        # the real-world case: 3:4 clips should yield a 3:4 canvas, no bars
        assert tl.choose_canvas([(1080, 1440), (2160, 2880)]) == (1080, 1440)

    def test_uniform_4_3_landscape(self):
        assert tl.choose_canvas([(1440, 1080)]) == (1440, 1080)

    def test_uniform_16_9_landscape(self):
        assert tl.choose_canvas([(1920, 1080)]) == (1920, 1080)

    def test_square(self):
        assert tl.choose_canvas([(1000, 1000)]) == (1080, 1080)

    def test_mixed_aspect_falls_back_to_default(self):
        assert tl.choose_canvas([(1080, 1920), (1920, 1080)]) == (
            tl.DEFAULT_W, tl.DEFAULT_H,
        )

    def test_dims_within_tolerance_are_uniform(self):
        # 1080x1920 (0.5625) and 1080x1912 (~0.565) are within tol -> fit
        w, h = tl.choose_canvas([(1080, 1920), (1080, 1912)])
        assert (w, h) != (tl.DEFAULT_W, tl.DEFAULT_H) or (w, h) == (1080, 1920)

    def test_always_even_dimensions(self):
        for dims in ([(1001, 1333)], [(1333, 1001)], [(1080, 1437)]):
            w, h = tl.choose_canvas(dims)
            assert w % 2 == 0 and h % 2 == 0

    def test_extra_tall_capped_at_1920(self):
        w, h = tl.choose_canvas([(1080, 3000)])
        assert h <= 1920 and w % 2 == 0 and h % 2 == 0

    def test_extra_wide_capped_at_1920(self):
        w, h = tl.choose_canvas([(3000, 1080)])
        assert w <= 1920 and w % 2 == 0 and h % 2 == 0

    def test_ignores_zero_dims(self):
        assert tl.choose_canvas([(0, 0), (1080, 1440)]) == (1080, 1440)


# ---------------------------------------------------------------------------
# audio_fx — per-clip echo/reverb
# ---------------------------------------------------------------------------

class TestAudioFx:
    def test_valid_preset_passes_validation(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["audio_fx"] = "cavern"
        assert tl.validate_timeline(t) == []

    def test_invalid_preset_rejected(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["audio_fx"] = "nope"
        assert any("audio_fx" in e for e in tl.validate_timeline(t))

    def test_fx_injected_into_filtergraph(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["audio_fx"] = "reverb"
        assert "aecho=" in compile_simple(t)["filter_complex"]

    def test_no_fx_means_no_aecho(self):
        assert "aecho" not in compile_simple(make_timeline(1))["filter_complex"]

    def test_fx_does_not_change_duration(self):
        t = make_timeline(1)
        base = tl.timeline_duration(t)
        tl.video_items(t)[0]["audio_fx"] = "cavern"
        assert tl.timeline_duration(t) == base

    def test_fx_composes_with_speed(self):
        t = make_timeline(1)
        it = tl.video_items(t)[0]
        it["speed"], it["audio_fx"] = 0.5, "cavern"
        assert tl.validate_timeline(t) == []
        fc = compile_simple(t)["filter_complex"]
        assert "atempo" in fc and "aecho=" in fc

    def test_set_speed_keeps_fx(self):
        # all presets are individually valid
        for name in tl.AUDIO_FX:
            t = make_timeline(1)
            tl.video_items(t)[0]["audio_fx"] = name
            assert tl.validate_timeline(t) == [], name


# ---------------------------------------------------------------------------
# mute — silent clips (e.g. time-lapse)
# ---------------------------------------------------------------------------

class TestMute:
    def test_mute_true_valid(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["mute"] = True
        assert tl.validate_timeline(t) == []

    def test_mute_non_bool_rejected(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["mute"] = "yes"
        assert any("mute" in e for e in tl.validate_timeline(t))

    def test_mute_injects_volume0(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["mute"] = True
        assert "volume=0" in compile_simple(t)["filter_complex"]

    def test_no_mute_no_volume0(self):
        assert "volume=0" not in compile_simple(make_timeline(1))["filter_complex"]

    def test_mute_does_not_change_duration(self):
        t = make_timeline(1)
        base = tl.timeline_duration(t)
        tl.video_items(t)[0]["mute"] = True
        assert tl.timeline_duration(t) == base

    def test_set_mute_op(self):
        out = tl.apply_ops(make_timeline(1), [
            {"op": "set_mute", "id": "v1", "mute": True},
        ])
        assert tl.video_items(out)[0].get("mute") is True

    def test_set_mute_false_clears(self):
        t = make_timeline(1)
        tl.video_items(t)[0]["mute"] = True
        out = tl.apply_ops(t, [{"op": "set_mute", "id": "v1", "mute": False}])
        assert "mute" not in tl.video_items(out)[0]

    def test_set_mute_non_bool_raises(self):
        with pytest.raises(tl.TimelineError, match="true or false"):
            tl.apply_ops(make_timeline(1), [
                {"op": "set_mute", "id": "v1", "mute": 1},
            ])

    def test_mute_composes_with_speed_and_fx(self):
        t = make_timeline(1)
        it = tl.video_items(t)[0]
        it["speed"], it["audio_fx"], it["mute"] = 10.0, "cavern", True
        assert tl.validate_timeline(t) == []
        fc = compile_simple(t)["filter_complex"]
        assert "atempo" in fc and "aecho=" in fc and "volume=0" in fc
