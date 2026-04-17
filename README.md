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

## Docker

If you'd rather not install `ffmpeg` and the torch/whisperx stack on your
host, there's a `Dockerfile` that bundles everything. The image is CPU-only
(~660 MB — the wav2vec2 model downloads to a mounted cache volume on first
run, not baked into the image) and uses `python:3.11-slim` as the base.

```bash
# Build once. Takes a while — whisperx pulls torch, transformers, etc.
docker build -t lyricsync:latest .
```

The typical invocation mounts your current directory at `/work` inside the
container and persists the wav2vec2 / HuggingFace model cache in a named
volume so you don't re-download ~1 GB on every run:

```bash
# Using the wrapper script (recommended):
./run.sh align video.mp4 lyrics.txt --output-dir ./out

# Or the raw docker run equivalent:
docker run --rm -it \
    -v "$PWD":/work \
    -v lyricsync-cache:/cache \
    lyricsync:latest align video.mp4 lyrics.txt --output-dir ./out
```

The first alignment run will download the wav2vec2 phoneme model (several
hundred MB) into the `lyricsync-cache` volume — expect a minute or two of
network time before alignment actually starts. Subsequent runs reuse the
cache and skip the download.

GPU support is a v1+ concern; the image ships CPU-only torch wheels
(`--extra-index-url https://download.pytorch.org/whl/cpu`) to keep the
image from ballooning past 5 GB with CUDA libraries we can't use anyway.

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
