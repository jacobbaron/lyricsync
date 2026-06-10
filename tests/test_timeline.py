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
    return tl.compile_timeline(
        timeline,
        resolve_source=lambda src: f"/cache/{src}",
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
        tl.video_items(timeline)[0]["speed"] = 10.0
        assert any("speed" in e for e in tl.validate_timeline(timeline))

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
                {"op": "set_speed", "id": "v1", "speed": 8.0},
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

    def test_resolver_receives_source_names(self):
        seen = []

        def resolver(src):
            seen.append(src)
            return f"/x/{src}"

        tl.compile_timeline(
            make_timeline(2), resolver, workdir="/w", font_path="/f.ttf",
        )
        assert seen == ["IMG_0001.mov", "IMG_0001.mov"]
