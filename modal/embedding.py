"""Pure helpers for the clip/frame embedding track (PERCEPTION T4).

No Modal/torch/network deps — stdlib only — so it imports cleanly in tests
(mirrors quality.py / motion.py). The model inference (CLIP image/text
encoding) lives in app.py; the pooling, normalization, and pgvector-literal
shaping that surround it live here and are unit-tested.

Model: sentence-transformers "clip-ViT-B-32" → a 512-dim space shared by the
image and text encoders, so a text query and a video frame are directly
comparable by cosine similarity. Keep EMBED_DIM in sync with the
`vector(512)` column in the clip_embeddings migration.
"""

from __future__ import annotations

import math

EMBED_MODEL = "clip-ViT-B-32"
EMBED_DIM = 512

# ~1 frame/sec is enough to index what's on screen without exploding row counts;
# cap total frames so a very long clip stays bounded.
EMBED_SAMPLE_FPS = 1.0
EMBED_MAX_FRAMES = 240


def frame_time(index: int, fps: float = EMBED_SAMPLE_FPS) -> float:
    """Clip-local timestamp (seconds) of the i-th sampled frame."""
    return round(index / fps, 3)


def l2_normalize(vec: list[float]) -> list[float]:
    """Return `vec` scaled to unit L2 norm (a zero vector is returned as-is).

    Normalizing makes cosine similarity a plain dot product and keeps every
    stored vector on the unit sphere, so the pooled clip vector is comparable
    to the per-frame ones.
    """
    norm = math.sqrt(sum(x * x for x in vec))
    if norm == 0.0:
        return list(vec)
    return [x / norm for x in vec]


def mean_pool(vectors: list[list[float]]) -> list[float]:
    """Mean-pool a list of equal-length vectors, then L2-normalize the result.

    Used to turn the per-frame frame vectors into a single whole-clip vector.
    Raises ValueError on empty input or ragged rows.
    """
    if not vectors:
        raise ValueError("cannot pool an empty list of vectors")
    dim = len(vectors[0])
    if any(len(v) != dim for v in vectors):
        raise ValueError("all vectors must have the same dimension")
    sums = [0.0] * dim
    for v in vectors:
        for i, x in enumerate(v):
            sums[i] += x
    n = len(vectors)
    return l2_normalize([s / n for s in sums])


def to_pgvector(vec: list[float]) -> str:
    """Format a float vector as a pgvector text literal: '[0.1,0.2,...]'.

    Postgres/PostgREST accept this string for a `vector` column or a cast
    (`$1::vector`), so it's how we pass embeddings in and out without a driver
    that knows the vector type natively.
    """
    return "[" + ",".join(repr(float(x)) for x in vec) + "]"
