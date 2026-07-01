"""Pure object-detection shaping helpers for the per-clip inventory signal
(PERCEPTION T5, closed-set YOLO test bed).

No Modal/torch/ultralytics/network deps — stdlib only — so it imports cleanly
in tests (mirrors quality.py / motion.py). The fragile part (ffmpeg frame
sampling + YOLO inference) lives in app.py and feeds its raw per-frame
detections through the pure functions here, which do the tracking (stitch
per-frame boxes into object tracklets) and the per-clip inventory shaping.

Raw per-frame detection input (one entry per sampled frame, in time order):

    {"t": 0.5, "detections": [
        {"class": "person", "conf": 0.91, "box": [x1, y1, x2, y2]},
        {"class": "chair",  "conf": 0.77, "box": [x1, y1, x2, y2]},
    ]}

Boxes are pixel corners [x1, y1, x2, y2] in the sampled-frame space.

Stored sidecar JSON (R2: projects/<pid>/clips/<cid>/detections.json) keeps the
full per-frame boxes + tracklets; `clip_signals.result` keeps just the compact
inventory + summary.
"""

from __future__ import annotations

DETECTION_VERSION = 1

DEFAULT_IOU_THRESHOLD = 0.3   # min IoU to link a box to an existing tracklet
DEFAULT_MAX_GAP_FRAMES = 2    # keep a tracklet alive across this many missed frames


def iou(a: list[float], b: list[float]) -> float:
    """Intersection-over-union of two [x1, y1, x2, y2] boxes (0.0 if disjoint)."""
    ax1, ay1, ax2, ay2 = a
    bx1, by1, bx2, by2 = b
    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    iw, ih = max(0.0, ix2 - ix1), max(0.0, iy2 - iy1)
    inter = iw * ih
    area_a = max(0.0, ax2 - ax1) * max(0.0, ay2 - ay1)
    area_b = max(0.0, bx2 - bx1) * max(0.0, by2 - by1)
    union = area_a + area_b - inter
    return inter / union if union > 0 else 0.0


def track_detections(
    frames: list[dict],
    iou_threshold: float = DEFAULT_IOU_THRESHOLD,
    max_gap_frames: int = DEFAULT_MAX_GAP_FRAMES,
) -> list[dict]:
    """Greedy IoU tracker: stitch per-frame boxes into per-object tracklets.

    Each sampled detection is linked to the best-overlapping active tracklet of
    the SAME class (IoU ≥ threshold); unmatched detections seed new tracklets.
    A tracklet that goes unmatched for more than `max_gap_frames` frames is
    closed, so brief misses don't fragment one object but a real disappearance
    ends the track.

    Returns tracklets in creation order:
      {"id", "class", "boxes": [{"t", "box", "conf"}], "start", "end"}
    """
    active: list[dict] = []   # tracklets still eligible to match
    finished: list[dict] = []
    next_id = 0

    for i, frame in enumerate(frames):
        dets = frame.get("detections", [])
        used_tracks: set[int] = set()
        used_dets: set[int] = set()

        # All plausible (track, det) links this frame, best IoU first.
        candidates = []
        for ti, tr in enumerate(active):
            for di, det in enumerate(dets):
                if det["class"] != tr["class"]:
                    continue
                score = iou(tr["boxes"][-1]["box"], det["box"])
                if score >= iou_threshold:
                    candidates.append((score, ti, di))
        candidates.sort(reverse=True)

        for _score, ti, di in candidates:
            if ti in used_tracks or di in used_dets:
                continue
            used_tracks.add(ti)
            used_dets.add(di)
            det = dets[di]
            tr = active[ti]
            tr["boxes"].append({"t": frame["t"], "box": det["box"], "conf": det["conf"]})
            tr["last_idx"] = i

        # Unmatched detections start new tracklets.
        for di, det in enumerate(dets):
            if di in used_dets:
                continue
            active.append({
                "id": next_id,
                "class": det["class"],
                "boxes": [{"t": frame["t"], "box": det["box"], "conf": det["conf"]}],
                "last_idx": i,
            })
            next_id += 1

        # Retire tracklets that have gone quiet for longer than the allowed gap.
        still_active = []
        for tr in active:
            if i - tr["last_idx"] > max_gap_frames:
                finished.append(tr)
            else:
                still_active.append(tr)
        active = still_active

    finished.extend(active)
    finished.sort(key=lambda tr: tr["id"])

    for tr in finished:
        tr.pop("last_idx", None)
        tr["start"] = round(tr["boxes"][0]["t"], 3)
        tr["end"] = round(tr["boxes"][-1]["t"], 3)
    return finished


def _mean_box(boxes: list[list[float]]) -> list[float]:
    n = len(boxes)
    return [round(sum(b[k] for b in boxes) / n, 1) for k in range(4)]


def summarize_tracklet(tr: dict) -> dict:
    """Compact per-tracklet summary (drops the per-frame box list)."""
    confs = [b["conf"] for b in tr["boxes"]]
    return {
        "id": tr["id"],
        "class": tr["class"],
        "start": tr["start"],
        "end": tr["end"],
        "n_frames": len(tr["boxes"]),
        "mean_conf": round(sum(confs) / len(confs), 3),
        "mean_box": _mean_box([b["box"] for b in tr["boxes"]]),
    }


def build_inventory(
    frames: list[dict], tracklets: list[dict], fps_sampled: float,
) -> dict:
    """Per-class inventory: distinct-object count, on-screen time, confidence.

    - count       — number of tracklets of the class (distinct object instances).
    - screen_time — seconds the class is visible = (frames it appears in) / fps.
    - mean_conf / mean_box — averaged over every detection of the class.
    """
    counts: dict[str, int] = {}
    for tr in tracklets:
        counts[tr["class"]] = counts.get(tr["class"], 0) + 1

    frames_present: dict[str, int] = {}
    confs: dict[str, list[float]] = {}
    boxes: dict[str, list[list[float]]] = {}
    for frame in frames:
        seen = set()
        for det in frame.get("detections", []):
            c = det["class"]
            confs.setdefault(c, []).append(det["conf"])
            boxes.setdefault(c, []).append(det["box"])
            seen.add(c)
        for c in seen:
            frames_present[c] = frames_present.get(c, 0) + 1

    inventory: dict[str, dict] = {}
    for c in counts:
        inventory[c] = {
            "count": counts[c],
            "screen_time": round(frames_present.get(c, 0) / fps_sampled, 2),
            "frames_present": frames_present.get(c, 0),
            "mean_conf": round(sum(confs[c]) / len(confs[c]), 3),
            "mean_box": _mean_box(boxes[c]),
        }
    return inventory


def build_detection_result(
    frames: list[dict], tracklets: list[dict], fps_sampled: float, model: str,
) -> dict:
    """Compact result for the `clip_signals.result` column (inventory + summary)."""
    inventory = build_inventory(frames, tracklets, fps_sampled)
    classes = sorted(inventory, key=lambda c: inventory[c]["screen_time"], reverse=True)
    return {
        "model": model,
        "inventory": inventory,
        "summary": {
            "classes": classes,
            "n_tracklets": len(tracklets),
            "n_frames": len(frames),
        },
    }


def build_detection_doc(
    frames: list[dict], duration: float, fps_sampled: float, model: str,
    tracklets: list[dict] | None = None,
) -> dict:
    """Assemble the full stored sidecar document (per-frame boxes + tracklets)."""
    if tracklets is None:
        tracklets = track_detections(frames)
    inventory = build_inventory(frames, tracklets, fps_sampled)
    return {
        "version": DETECTION_VERSION,
        "duration": round(float(duration), 3),
        "fps_sampled": fps_sampled,
        "model": model,
        "n_frames": len(frames),
        "frames": frames,
        "tracklets": [summarize_tracklet(tr) for tr in tracklets],
        "inventory": inventory,
    }
