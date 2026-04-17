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
    help="Force-align lyrics to a music video; SRT, preview MP4, and optional timing editor.",
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

    typer.echo(f"[1/5] Extracting audio -> {audio_wav}")
    extract_audio(video, audio_wav)

    typer.echo(f"[2/5] Parsing lyrics <- {lyrics_txt}")
    lyrics = parse_lyrics(lyrics_txt)
    typer.echo(
        f"       {len(lyrics.lines)} lines, "
        f"{len(lyrics.flat_words)} words"
    )

    typer.echo("[3/5] Running WhisperX forced alignment")
    result = run_whisperx_align(
        audio_path=audio_wav,
        lyrics=lyrics,
        device=device,
        language=language,
    )

    typer.echo(f"[4/5] Writing alignment.json + SRT")
    from .alignment_json import write_alignment_json

    meta = {
        "source_video": str(video.resolve()),
        "lyrics_txt": str(lyrics_txt.resolve()),
    }
    alignment_json_path = output_dir / "alignment.json"
    write_alignment_json(result, alignment_json_path, meta=meta)
    typer.echo(f"       {alignment_json_path}")
    write_srt(result, srt_path)
    typer.echo(f"       {srt_path}")

    typer.echo(f"[5/5] Rendering preview -> {preview_path}")
    render_preview(video, result, preview_path)

    typer.echo(
        f"Done. {len(result.lines)} lines aligned. "
        f"Edit timings: `lyricsync serve -o {output_dir}` (requires editor extras)."
    )


@app.command()
def render(
    video: Path = typer.Argument(
        ...,
        exists=True,
        dir_okay=False,
        readable=True,
        help="Input video (same as used for align).",
    ),
    output_dir: Path = typer.Option(
        Path("./out"),
        "--output-dir",
        "-o",
        help="Directory containing alignment.json (and writes captions + preview here).",
    ),
    alignment: Path | None = typer.Option(
        None,
        "--alignment",
        "-a",
        help="Path to alignment.json (default: OUTPUT_DIR/alignment.json).",
    ),
) -> None:
    """Rebuild captions.srt and preview.mp4 from a saved alignment.json."""
    from .alignment_json import read_alignment_json
    from .preview import render_preview
    from .srt import write_srt

    path = alignment or (output_dir / "alignment.json")
    if not path.is_file():
        typer.echo(f"Error: alignment file not found: {path}", err=True)
        raise typer.Exit(1)
    result = read_alignment_json(path)
    srt_path = output_dir / "captions.srt"
    preview_path = output_dir / "preview.mp4"
    output_dir.mkdir(parents=True, exist_ok=True)
    typer.echo(f"Writing SRT -> {srt_path}")
    write_srt(result, srt_path)
    typer.echo(f"Rendering preview -> {preview_path}")
    render_preview(video, result, preview_path)
    typer.echo("Done.")


@app.command()
def serve(
    output_dir: Path = typer.Option(
        Path("./out"),
        "--output-dir",
        "-o",
        help="Project directory with alignment.json and audio.wav.",
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        help="Bind address (default: loopback only).",
    ),
    port: int = typer.Option(
        8765,
        "--port",
        help="HTTP port for the timing editor API + UI.",
    ),
) -> None:
    """Serve the timing editor UI and API for an align output directory."""
    try:
        from .serve_app import run_server
    except ImportError as exc:  # pragma: no cover
        typer.echo(
            "Missing dependencies for `serve`. Install with:\n"
            "  uv sync --extra editor\n"
            "or: pip install 'lyricsync[editor]'",
            err=True,
        )
        raise typer.Exit(1) from exc
    output_dir = output_dir.resolve()
    if not (output_dir / "alignment.json").is_file():
        typer.echo(
            f"Error: {output_dir / 'alignment.json'} not found. Run `lyricsync align` first.",
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(
        f"Timing editor: http://{host}:{port}/\n"
        f"API: GET/PUT http://{host}:{port}/api/alignment"
    )
    run_server(output_dir, host=host, port=port)


if __name__ == "__main__":  # pragma: no cover
    app()
