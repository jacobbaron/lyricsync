"""Tests for the animation renderer registry (animation/__init__.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from lyricsync.alignment import AlignmentResult
from lyricsync.animation import (
    AnimationRenderer,
    available_renderers,
    get_renderer,
    register_renderer,
)
from lyricsync.animation.styles import StylePreset


class TestRendererRegistry:
    def test_ass_registered_at_import_time(self):
        assert "ass" in available_renderers()

    def test_get_renderer_returns_ass(self):
        renderer = get_renderer("ass")
        assert renderer.name == "ass"
        assert isinstance(renderer, AnimationRenderer)

    def test_unknown_renderer_raises(self):
        with pytest.raises(KeyError, match="unknown animation backend"):
            get_renderer("nonexistent_backend")

    def test_error_lists_available(self):
        with pytest.raises(KeyError, match="ass"):
            get_renderer("bad")

    def test_available_renderers_sorted(self):
        names = available_renderers()
        assert names == sorted(names)


class _FakeRenderer:
    name = "fake_test_backend"

    def write_caption_file(
        self,
        result: AlignmentResult,
        style: StylePreset,
        out_path: Path,
        video_size: tuple[int, int] | None = None,
    ) -> Path:
        return out_path

    def ffmpeg_video_filter(self, caption_path: Path) -> str:
        return "null"


class TestRegisterRenderer:
    def test_register_and_retrieve(self):
        fake = _FakeRenderer()
        register_renderer(fake)
        assert get_renderer("fake_test_backend") is fake
        assert "fake_test_backend" in available_renderers()

    def test_custom_renderer_satisfies_protocol(self):
        fake = _FakeRenderer()
        assert isinstance(fake, AnimationRenderer)
