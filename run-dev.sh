#!/usr/bin/env bash
# Docker wrapper like run.sh, but runs the repo source from the bind mount
# (PYTHONPATH=/work/src) so you can iterate on Python without rebuilding the image.
#
# Usage (same as run.sh):
#   ./run-dev.sh align VIDEO LYRICS_TXT [--output-dir out] [...more flags]
#
# Rebuild the image only when dependencies change (pyproject.toml / uv.lock).

set -euo pipefail

IMAGE="${LYRICSYNC_IMAGE:-lyricsync:latest}"
CACHE_VOLUME="${LYRICSYNC_CACHE_VOLUME:-lyricsync-cache}"

exec docker run --rm -it \
    -e PYTHONPATH=/work/src \
    -v "$PWD":/work \
    -v "$CACHE_VOLUME":/cache \
    -w /work \
    --entrypoint python \
    "$IMAGE" \
    -m lyricsync.cli \
    "$@"
