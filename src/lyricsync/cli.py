"""Typer CLI entrypoint.

v0 exposes a single command:

    lyricsync align VIDEO LYRICS_TXT [--output-dir ./out]

Future commands (``batch``, ``render``) land in later phases.
"""

from __future__ import annotations

from pathlib import Path

import typer

from . import __version__

app = typer.Typer(
    add_completion=False,
    help="Force-align lyrics to a music video. v0 = SRT + preview MP4.",
)


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"lyricsync {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False,
        "--version",
        callback=_version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    """lyricsync — forced-alignment captioning for music videos."""


@app.command()
def align(
    video: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Input video file (any format ffmpeg can read).",
    ),
    lyrics_txt: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Plaintext lyrics, one caption line per line, blank lines = gaps.",
    ),
    output_dir: Path = typer.Option(
        Path("./out"),
        "--output-dir",
        "-o",
        help="Where to write captions.srt and preview.mp4.",
    ),
    device: str = typer.Option(
        "cpu",
        "--device",
        help="Torch device for the alignment model (cpu or cuda).",
    ),
    language: str = typer.Option(
        "en",
        "--language",
        help="Language code for the wav2vec2 alignment model.",
    ),
) -> None:
    """Align LYRICS_TXT to VIDEO and write captions.srt + preview.mp4."""
    # Imports deferred: whisperx+torch imports are slow, and we don't
    # want `--help` to pay that cost.
    from .align import run_whisperx_align
    from .extract import extract_audio
    from .lyrics import parse_lyrics
    from .preview import render_preview
    from .srt import write_srt

    output_dir.mkdir(parents=True, exist_ok=True)
    audio_wav = output_dir / "audio.wav"
    srt_path = output_dir / "captions.srt"
    preview_path = output_dir / "preview.mp4"

    typer.echo(f"[1/4] Extracting audio -> {audio_wav}")
    extract_audio(video, audio_wav)

    typer.echo(f"[2/4] Parsing lyrics <- {lyrics_txt}")
    lyrics = parse_lyrics(lyrics_txt)
    typer.echo(
        f"       {len(lyrics.lines)} lines, "
        f"{len(lyrics.flat_words)} words"
    )

    typer.echo("[3/4] Running WhisperX forced alignment")
    result = run_whisperx_align(
        audio_path=audio_wav,
        lyrics=lyrics,
        device=device,
        language=language,
    )

    typer.echo(f"[4/4] Writing SRT -> {srt_path}")
    write_srt(result, srt_path)

    typer.echo(f"       Rendering preview -> {preview_path}")
    render_preview(video, result, preview_path)

    typer.echo(f"Done. {len(result.lines)} lines aligned.")


if __name__ == "__main__":  # pragma: no cover
    app()
