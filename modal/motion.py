"""Pure camera-motion / shot-dynamics helpers for the per-clip pacing signal.

No Modal/ffmpeg/cv2/network deps — stdlib only — so it imports cleanly in
tests (mirrors transcript.py / quality.py). The fragile parts (ffmpeg frame
sampling, OpenCV optical-flow estimation, ffmpeg scene detection) live in
app.py and feed their raw per-frame motion numbers through the pure functions
here, which do the bucketing / labeling / shaping.

These are the pacing / cut-point signals a sparse-frame VLM misses: push-ins
read as emphasis, whip-pans and static onsets are natural cut points, handheld
vs locked-off changes the feel of a shot.

Per-frame motion vector (computed in the worker, one per sampled frame after
the first):

    {"t": 0.33, "dx": 1.2, "dy": -0.3, "scale": 0.004, "mag": 1.6}

  dx, dy — median global translation (px) vs the previous frame: + dx = scene
           moving right (camera pans left) etc. Sign is kept; we use the
           coherent translation magnitude hypot(dx, dy) as the primary motion
           discriminator and dx vs dy to pick pan vs tilt.
  scale  — mean radial divergence of the flow field: > 0 = field expands
           outward (push-in / zoom-in), < 0 = contracts (pull-out / zoom-out).
  mag    — mean flow magnitude (overall motion intensity, unsigned). Kept for
           reference only — dense optical flow has a large per-pixel noise
           floor (texture / compression / rolling shutter), so `mag` sits near
           ~10 even on a locked-off shot and can't separate static from moving.
           Classification keys off the coherent translation instead.

Stored sidecar JSON shape (R2: projects/<pid>/clips/<cid>/motion.json):

    {
      "version": 1,
      "duration": 12.4,
      "fps_sampled": 3.0,
      "seconds": [{"t": 0, "dx": 0.1, "dy": 0.0, "scale": 0.0,
                   "mag": 0.2, "jitter": 0.1, "label": "static"}, ...],
      "spans": [{"start": 0, "end": 4, "label": "static"},
                {"start": 4, "end": 9, "label": "pan"}, ...],
      "scene_cuts": [4.2, 9.0],
      "summary": {"dominant": "pan", "label_seconds": {"static": 4, "pan": 5},
                  "n_scene_cuts": 2}
    }

`clip_signals.result` stores just `summary` + `spans` + `scene_cuts` (compact);
the full per-second timeline lives in the R2 sidecar (`result_r2_key`).
"""

from __future__ import annotations

import re

MOTION_VERSION = 1

# Labels we classify a second of footage into.
LABELS = ("static", "pan", "tilt", "zoom_in", "zoom_out", "handheld", "whip")

# Thresholds on the coherent translation net = hypot(dx, dy), in px/frame on a
# 320px-wide sampled frame at ~3 fps. Calibrated against real footage (a Home
# Depot aisle walk vs a handheld talking-head): a held shot sits at net < ~2,
# deliberate pans run ~3.5–12, and a whip flick spikes past ~13. Easy to retune
# without touching the shaping logic below.
STATIC_NET = 2.0        # coherent translation below this = camera essentially held
PAN_NET = 3.5           # net at/above this = a deliberate directional pan/tilt
WHIP_NET = 13.0         # net at/above this = whip / very fast move
ZOOM_SCALE = 0.035      # |mean radial divergence| above this (and small net) = zoom
HANDHELD_JITTER = 6.0   # in-second std(dx)+std(dy) above this (no net dir) = handheld


def _mean(xs: list[float]) -> float:
    return sum(xs) / len(xs) if xs else 0.0


def _std(xs: list[float]) -> float:
    """Population standard deviation (0 for <2 samples)."""
    if len(xs) < 2:
        return 0.0
    m = _mean(xs)
    return (sum((x - m) ** 2 for x in xs) / len(xs)) ** 0.5


def bucket_by_second(samples: list[dict], duration: float) -> list[dict]:
    """Aggregate per-frame motion vectors into one bucket per whole second.

    Each bucket carries the second's mean dx/dy/scale/mag plus a `jitter` term
    (std of dx + std of dy within the second) that separates shaky handheld
    motion from a smooth directional pan. Seconds with no samples are omitted
    rather than fabricated, so sampling gaps don't read as "static".
    """
    n_seconds = max(1, int(duration) + 1)
    buckets: list[list[dict]] = [[] for _ in range(n_seconds)]
    for s in samples:
        idx = int(s["t"])
        if 0 <= idx < n_seconds:
            buckets[idx].append(s)

    out: list[dict] = []
    for i, bucket in enumerate(buckets):
        if not bucket:
            continue
        dxs = [x["dx"] for x in bucket]
        dys = [x["dy"] for x in bucket]
        out.append({
            "t": i,
            "dx": round(_mean(dxs), 4),
            "dy": round(_mean(dys), 4),
            "scale": round(_mean([x["scale"] for x in bucket]), 5),
            "mag": round(_mean([x["mag"] for x in bucket]), 4),
            "jitter": round(_std(dxs) + _std(dys), 4),
        })
    return out


def classify_bucket(b: dict) -> str:
    """Label one second of motion from its aggregated metrics.

    Driven by the coherent translation `net = hypot(dx, dy)` (robust, median-
    based) rather than the noisy mean flow magnitude. Order: a whip flick (very
    large net) first; then a zoom, but only when translation is small so a real
    pan with incidental radial noise isn't misread as a zoom; then a directional
    pan/tilt; finally static vs handheld for low-translation seconds (handheld =
    motion energy / jitter without a sustained direction).
    """
    net = (b["dx"] ** 2 + b["dy"] ** 2) ** 0.5
    if net >= WHIP_NET:
        return "whip"
    if abs(b["scale"]) >= ZOOM_SCALE and net < PAN_NET:
        return "zoom_in" if b["scale"] > 0 else "zoom_out"
    if net >= PAN_NET:
        return "pan" if abs(b["dx"]) >= abs(b["dy"]) else "tilt"
    if net >= STATIC_NET or b["jitter"] >= HANDHELD_JITTER:
        return "handheld"
    return "static"


def label_seconds(buckets: list[dict]) -> list[dict]:
    """Attach a `label` to each per-second bucket from its own metrics."""
    return [{**b, "label": classify_bucket(b)} for b in buckets]


def merge_label_spans(seconds: list[dict]) -> list[dict]:
    """Merge contiguous same-label seconds into spans that tile the timeline.

    A span breaks on a label change OR a gap in the second index, so spans
    never bridge an un-sampled hole.
    """
    spans: list[dict] = []
    cur: dict | None = None
    for s in seconds:
        if cur is not None and s["label"] == cur["label"] and s["t"] == cur["end"]:
            cur["end"] = s["t"] + 1
        else:
            if cur is not None:
                spans.append(cur)
            cur = {"start": s["t"], "end": s["t"] + 1, "label": s["label"]}
    if cur is not None:
        spans.append(cur)
    return spans


def summarize(spans: list[dict], scene_cuts: list[float], duration: float) -> dict:
    """Compact summary for the `clip_signals.result` column."""
    label_seconds: dict[str, int] = {}
    for sp in spans:
        secs = sp["end"] - sp["start"]
        label_seconds[sp["label"]] = label_seconds.get(sp["label"], 0) + secs
    dominant = max(label_seconds, key=label_seconds.get) if label_seconds else None
    return {
        "dominant": dominant,
        "label_seconds": label_seconds,
        "n_scene_cuts": len(scene_cuts),
    }


def build_motion_doc(
    duration: float,
    fps_sampled: float,
    samples: list[dict],
    scene_cuts: list[float] | None = None,
) -> dict:
    """Assemble the stored motion sidecar document (see module docstring)."""
    scene_cuts = sorted(scene_cuts or [])
    buckets = bucket_by_second(samples, duration)
    seconds = label_seconds(buckets)
    spans = merge_label_spans(seconds)
    summary = summarize(spans, scene_cuts, duration)
    return {
        "version": MOTION_VERSION,
        "duration": round(float(duration), 3),
        "fps_sampled": fps_sampled,
        "seconds": seconds,
        "spans": spans,
        "scene_cuts": scene_cuts,
        "summary": summary,
    }


_PTS_TIME_RE = re.compile(r"pts_time:\s*([\d.]+)")


def parse_scene_cuts(stderr: str, min_gap: float = 0.5) -> list[float]:
    """Parse shot-boundary times from ffmpeg `select=gt(scene,...),showinfo`.

    The select filter only passes frames whose scene score exceeds the
    threshold; `showinfo` then prints one line per passed frame carrying its
    `pts_time:`. We collect those times, drop near-duplicates within `min_gap`
    seconds (a single cut can trip two adjacent frames), and return them sorted.
    """
    times = sorted(
        float(m.group(1)) for m in _PTS_TIME_RE.finditer(stderr or "")
    )
    cuts: list[float] = []
    for t in times:
        if not cuts or t - cuts[-1] >= min_gap:
            cuts.append(round(t, 3))
    return cuts
