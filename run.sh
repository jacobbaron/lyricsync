#!/usr/bin/env bash
# Thin wrapper around `docker run` for the typical lyricsync invocation.
#
# Usage:
#   ./run.sh align VIDEO LYRICS_TXT [--output-dir out] [...more flags]
#
# Conventions:
#   - $PWD is bind-mounted at /work inside the container, so VIDEO and
#     LYRICS_TXT paths are interpreted relative to wherever you invoke
#     this script from (same mental model as running `lyricsync` directly).
#   - A named volume `lyricsync-cache` persists the HuggingFace /
#     wav2vec2 model cache across runs. First run downloads ~1GB; later
#     runs are instant.
#
# If you need to pass explicit absolute host paths, call `docker run`
# directly — this script is a convenience, not a contract.

set -euo pipefail

IMAGE="${LYRICSYNC_IMAGE:-lyricsync:latest}"
CACHE_VOLUME="${LYRICSYNC_CACHE_VOLUME:-lyricsync-cache}"

# Allocate a TTY only when stdin is one. `-it` against a piped or
# non-TTY stdin errors out with "the input device is not a TTY".
TTY_FLAGS=()
if [[ -t 0 && -t 1 ]]; then
    TTY_FLAGS=(-it)
fi

exec docker run --rm "${TTY_FLAGS[@]}" \
    -v "$PWD":/work \
    -v "$CACHE_VOLUME":/cache \
    -w /work \
    "$IMAGE" "$@"
