"""Tests for animation/styles.py — presets and registry."""

from __future__ import annotations

import pytest

from lyricsync.animation.styles import (
    BUILTIN_PRESETS,
    StylePreset,
    available_presets,
    get_preset,
)


class TestGetPreset:
    def test_returns_known_presets(self):
        for name in ("classic", "pop", "neon", "plain"):
            preset = get_preset(name)
            assert isinstance(preset, StylePreset)
            assert preset.name == name

    def test_unknown_preset_raises_key_error(self):
        with pytest.raises(KeyError, match="unknown style preset"):
            get_preset("nonexistent")

    def test_error_message_lists_available(self):
        with pytest.raises(KeyError, match="classic"):
            get_preset("bad_name")


class TestAvailablePresets:
    def test_returns_sorted_names(self):
        names = available_presets()
        assert names == sorted(names)
        assert set(names) == set(BUILTIN_PRESETS)

    def test_all_builtins_are_listed(self):
        assert set(available_presets()) == {"classic", "neon", "plain", "pop"}


class TestStylePresetDefaults:
    def test_classic_defaults(self):
        preset = get_preset("classic")
        assert preset.font_name == "Arial"
        assert preset.font_size == 48
        assert preset.bold is False
        assert preset.word_animation == "karaoke"
        assert preset.primary_color == "#FFFFFF"
        assert preset.highlight_color == "#F5A623"

    def test_pop_overrides(self):
        preset = get_preset("pop")
        assert preset.font_name == "Impact"
        assert preset.bold is True
        assert preset.word_animation == "pop"

    def test_plain_disables_animation(self):
        preset = get_preset("plain")
        assert preset.word_animation == "none"
        assert preset.line_in == "none"
        assert preset.line_out == "none"

    def test_neon_settings(self):
        preset = get_preset("neon")
        assert preset.bold is True
        assert preset.word_animation == "karaoke"
        assert preset.shadow == 3.0

    def test_preset_is_frozen(self):
        preset = get_preset("classic")
        with pytest.raises(AttributeError):
            preset.name = "hacked"  # type: ignore[misc]

    def test_backend_params_default_empty(self):
        for name in available_presets():
            assert get_preset(name).backend_params == {}


class TestCustomPreset:
    def test_custom_creation(self):
        custom = StylePreset(
            name="custom",
            font_name="Helvetica",
            font_size=60,
            primary_color="#FF0000",
            highlight_color="#00FF00",
            outline_color="#0000FF",
            word_animation="fade",
        )
        assert custom.name == "custom"
        assert custom.font_name == "Helvetica"
        assert custom.word_animation == "fade"
