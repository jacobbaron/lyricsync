# syntax=docker/dockerfile:1.7
#
# lyricsync v0 container.
#
# CPU-only torch — v0 does not need GPU. See README for GPU notes (v1+).
# Multi-stage: the builder installs deps into /opt/venv using uv, then
# the runtime stage copies just that venv + ffmpeg. Keeps uv itself and
# build-time caches out of the final image.

ARG PYTHON_VERSION=3.11

# -----------------------------------------------------------------------------
# Stage 1: builder — install project deps into a venv using uv.
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS builder

# uv from its official static image; pin to a known-good tag.
COPY --from=ghcr.io/astral-sh/uv:0.5.11 /uv /uvx /usr/local/bin/

ENV UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    UV_PYTHON_DOWNLOADS=never \
    VIRTUAL_ENV=/opt/venv \
    # CPU-only torch: force uv/pip to resolve torch wheels from
    # pytorch.org's CPU index instead of PyPI's default CUDA-bundled
    # wheels. Saves ~2GB.
    UV_EXTRA_INDEX_URL=https://download.pytorch.org/whl/cpu \
    UV_INDEX_STRATEGY=unsafe-best-match

WORKDIR /app

# Create the target venv up front so uv pip installs into it.
RUN uv venv /opt/venv --python python${PYTHON_VERSION}

# Copy only what's needed for dependency resolution first, to maximize
# Docker layer cache hits across source-only edits.
COPY pyproject.toml uv.lock README.md ./
COPY src ./src

# Install the project + locked deps into /opt/venv.
# --no-dev skips the pytest extra; runtime image doesn't need it.
RUN --mount=type=cache,target=/root/.cache/uv \
    uv pip install --python /opt/venv/bin/python \
        --extra-index-url https://download.pytorch.org/whl/cpu \
        .

# -----------------------------------------------------------------------------
# Stage 2: runtime — slim Python + ffmpeg + the prebuilt venv.
# -----------------------------------------------------------------------------
FROM python:${PYTHON_VERSION}-slim AS runtime

# ffmpeg (audio extract + preview render). libsndfile1 is pulled in by
# soundfile/torchaudio on some platforms; cheap insurance.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ffmpeg \
        libsndfile1 \
    && rm -rf /var/lib/apt/lists/*

ENV VIRTUAL_ENV=/opt/venv \
    PATH="/opt/venv/bin:$PATH" \
    # Park HF / torch caches in /cache so the compose/run.sh wrapper
    # can mount a named volume there and avoid re-downloading the
    # wav2vec2 model on every run.
    HF_HOME=/cache/huggingface \
    TORCH_HOME=/cache/torch \
    XDG_CACHE_HOME=/cache \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

COPY --from=builder /opt/venv /opt/venv

# Default working dir for bind-mounted inputs/outputs.
WORKDIR /work

RUN mkdir -p /cache /work

ENTRYPOINT ["lyricsync"]
CMD ["--help"]
