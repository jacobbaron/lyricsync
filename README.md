# lyricsync

Forced-alignment captioning for music videos. Given a video and its
lyrics as plaintext, produce an SRT plus a verification preview MP4.

**Status:** v0 — end-to-end thin slice. See
[`lyric_aligner_spec.md`](./lyric_aligner_spec.md) for the full design.
v0 scope:

- Single CLI command: `lyricsync align VIDEO LYRICS_TXT`.
- Audio extraction via `ffmpeg` (16 kHz mono wav).
- Forced alignment via WhisperX's `align()` + wav2vec2 phoneme model,
  using the provided lyrics as the fixed transcript.
- Output: `captions.srt` and `preview.mp4` (original video with the
  current lyric line overlaid via `ffmpeg drawtext`).

No vocal separation, no plugin Protocols, no VTT/ASS/JSON, no
active-word highlighting yet — those are v1. The point of v0 is to
find out whether raw WhisperX alignment on a mixed-down music video is
good enough to justify the rest.

## Requirements

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) (recommended) or `pipx`
- `ffmpeg` on your `PATH` (`brew install ffmpeg` on macOS)

The WhisperX install pulls in `torch`, `torchaudio`, and `transformers`;
first run will download the wav2vec2 phoneme model to the HuggingFace
cache.

## Install

From a clone of this repo:

```bash
# Dev install with editable source + tests
uv sync --extra dev

# Or install as a user tool
uv tool install .
# ...or
pipx install .
```

## Usage

```bash
lyricsync align path/to/video.mp4 path/to/lyrics.txt
# → ./out/captions.srt
# → ./out/preview.mp4

lyricsync align video.mp4 lyrics.txt --output-dir ./my-out --device cuda
lyricsync align --help
```

**Lyrics file format.** One caption line per line. Blank lines are
preserved as gaps but aren't emitted as captions. No timestamps — that's
what the tool is for.

```
when the morning light
comes through the window

I'll be the one you're looking for
the one you're looking for
```

## Development

```bash
uv sync --extra dev
uv run pytest
uv run lyricsync align --help
```

The unit tests cover lyrics parsing, SRT formatting, and drawtext
filter construction. They do not exercise WhisperX (model download is
slow) or ffmpeg (needs fixture media). End-to-end validation is manual
in v0.

## Layout

```
src/lyricsync/
  cli.py         # typer entrypoint
  lyrics.py      # lyrics file parsing
  extract.py     # ffmpeg audio extraction
  align.py       # WhisperX wav2vec2 forced alignment
  alignment.py   # AlignmentResult dataclasses + word→line aggregation
  srt.py         # SRT writer
  preview.py     # drawtext preview renderer
tests/
```

Each stage is a plain function so v1 can slot in plugin `Protocol`s
(Separator, Aligner, OutputFormatter, PreviewRenderer) without a
rewrite.
