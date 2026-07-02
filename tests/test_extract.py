"""Tests for extract.py — ffmpeg detection and audio extraction.

No actual ffmpeg invocation; subprocess is mocked where needed.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from lyricsync.extract import FFmpegMissingError, extract_audio, require_ffmpeg


class TestRequireFfmpeg:
    def test_returns_path_when_found(self):
        with patch("shutil.which", return_value="/usr/bin/ffmpeg"):
            assert require_ffmpeg() == "/usr/bin/ffmpeg"

    def test_raises_when_not_found(self):
        with patch("shutil.which", return_value=None):
            with pytest.raises(FFmpegMissingError, match="ffmpeg not found"):
                require_ffmpeg()


class TestExtractAudio:
    def test_calls_ffmpeg_with_correct_args(self, tmp_path: Path):
        video = tmp_path / "input.mp4"
        video.touch()
        out_wav = tmp_path / "subdir" / "audio.wav"

        with (
            patch("lyricsync.extract.require_ffmpeg", return_value="/usr/bin/ffmpeg"),
            patch("subprocess.run") as mock_run,
        ):
            result = extract_audio(video, out_wav)

        assert result == out_wav
        assert out_wav.parent.exists()  # mkdir was called
        call_args = mock_run.call_args[0][0]
        assert call_args[0] == "/usr/bin/ffmpeg"
        assert "-y" in call_args
        assert str(video) in call_args
        assert str(out_wav) in call_args
        assert "16000" in call_args
        assert "pcm_s16le" in call_args
        mock_run.assert_called_once()
        assert mock_run.call_args[1].get("check") is True

    def test_raises_when_ffmpeg_missing(self, tmp_path: Path):
        with patch("shutil.which", return_value=None):
            with pytest.raises(FFmpegMissingError):
                extract_audio(tmp_path / "v.mp4", tmp_path / "a.wav")
