"""Pure technical-quality (QC) helpers for the per-clip usability signal.

No Modal/ffmpeg/cv2/network deps — stdlib only — so it imports cleanly in
tests (mirrors transcript.py / audio_analysis.py). The fragile parts (ffmpeg
frame sampling, OpenCV sharpness/exposure/motion metrics) live in app.py and
feed their raw per-frame numbers through the pure functions here, which do
the bucketing/scoring/shaping.

Stored sidecar JSON shape (R2: projects/<pid>/clips/<cid>/quality.json):

    {
      "version": 1,
      "duration": 56.82,
      "fps_sampled": 3.0,
      "seconds": [{"t": 0, "sharpness": 120.5, "black_frac": 0.0,
                   "white_frac": 0.01, "shake": 0.4, "usable": 1.0,
                   "reasons": []}, ...],
      "flagged_spans": [{"start": 2.0, "end": 3.0, "reasons": ["soft"]}, ...],
      "summary": {"mean_usable": 0.92, "flagged_seconds": 4,
                   "flagged_fraction": 0.07, "reasons_count": {"soft": 3}}
    }

`clip_signals.result` stores just `summary` + `flagged_spans` (compact); the
full per-second timeline lives in the R2 sidecar (`result_r2_key`).
"""

from __future__ import annotations

import re

QUALITY_VERSION = 1

# Thresholds (empirical starting points — easy to retune without touching the
# shaping logic below).
SHARPNESS_MIN = 40.0       # variance-of-Laplacian below this = soft/out-of-focus
BLACK_FRACTION_MAX = 0.85  # histogram fraction near-0 above this = crushed exposure
WHITE_FRACTION_MAX = 0.85  # histogram fraction near-255 above this = blown exposure
SHAKE_MAX = 12.0           # mean abs inter-frame diff above this = violent shake/handheld
FROZEN_MAX = 0.3           # mean abs inter-frame diff below this = frozen (not just static)


def bucket_by_second(samples: list[dict], duration: float) -> list[dict]:
    """Average per-frame metrics into one bucket per whole second.

    `samples` entries: {"t", "sharpness", "black_frac", "white_frac", "shake"}.
    Seconds with no samples are omitted (rather than fabricated), so gaps in
    sampling don't silently read as either "fine" or "unusable".
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
        out.append({
            "t": i,
            "sharpness": round(sum(x["sharpness"] for x in bucket) / len(bucket), 2),
            "black_frac": round(sum(x["black_frac"] for x in bucket) / len(bucket), 4),
            "white_frac": round(sum(x["white_frac"] for x in bucket) / len(bucket), 4),
            "shake": round(sum(x["shake"] for x in bucket) / len(bucket), 4),
        })
    return out


def flag_reasons(bucket: dict) -> list[str]:
    """Which QC issues a bucket's averaged metrics trip, if any."""
    reasons = []
    if bucket["sharpness"] < SHARPNESS_MIN:
        reasons.append("soft")
    if bucket["black_frac"] > BLACK_FRACTION_MAX:
        reasons.append("crushed")
    if bucket["white_frac"] > WHITE_FRACTION_MAX:
        reasons.append("blown")
    if bucket["shake"] > SHAKE_MAX:
        reasons.append("shake")
    elif bucket["shake"] < FROZEN_MAX:
        reasons.append("frozen")
    return reasons


def usable_score(reasons: list[str]) -> float:
    """1.0 = clean; each flagged issue knocks the score down, floor 0.0.

    "frozen"/"black" are full disqualifiers (handled by forcing 0.0 directly
    where applied); everything else stacks at -0.4 per issue.
    """
    if not reasons:
        return 1.0
    if "frozen" in reasons or "black" in reasons:
        return 0.0
    return round(max(0.0, 1.0 - 0.4 * len(reasons)), 2)


def score_seconds(buckets: list[dict]) -> list[dict]:
    """Attach `usable` + `reasons` to each bucket from its own metrics."""
    out = []
    for b in buckets:
        reasons = flag_reasons(b)
        out.append({**b, "usable": usable_score(reasons), "reasons": reasons})
    return out


def apply_forced_spans(seconds: list[dict], spans: list[dict], reason: str) -> None:
    """Mark seconds inside [start, end) `spans` (e.g. ffmpeg blackdetect) as
    forced-unusable with `reason`, in place. Inserts a stub bucket for any
    second with no prior sample so the span is fully represented.
    """
    by_t = {s["t"]: s for s in seconds}
    for span in spans:
        start, end = span["start"], span["end"]
        for t in range(int(start), max(int(start), int(end) - 1) + 1):
            sec = by_t.get(t)
            if sec is None:
                sec = {"t": t, "sharpness": None, "black_frac": None,
                       "white_frac": None, "shake": None, "usable": 0.0, "reasons": []}
                seconds.append(sec)
                by_t[t] = sec
            if reason not in sec["reasons"]:
                sec["reasons"].append(reason)
            sec["usable"] = 0.0
    seconds.sort(key=lambda s: s["t"])


def merge_flagged_spans(seconds: list[dict], threshold: float = 0.99) -> list[dict]:
    """Merge contiguous seconds with usable < `threshold` into spans, unioning
    each span's reasons across its constituent seconds.
    """
    spans: list[dict] = []
    cur: dict | None = None
    for s in seconds:
        if s["usable"] < threshold:
            if cur is not None and s["t"] == cur["end"]:
                cur["end"] = s["t"] + 1
                for r in s["reasons"]:
                    if r not in cur["reasons"]:
                        cur["reasons"].append(r)
            else:
                if cur is not None:
                    spans.append(cur)
                cur = {"start": s["t"], "end": s["t"] + 1, "reasons": list(s["reasons"])}
        elif cur is not None:
            spans.append(cur)
            cur = None
    if cur is not None:
        spans.append(cur)
    return spans


def summarize(seconds: list[dict], flagged_spans: list[dict], duration: float) -> dict:
    """Compact summary for the `clip_signals.result` column."""
    scores = [s["usable"] for s in seconds]
    mean_usable = round(sum(scores) / len(scores), 3) if scores else 1.0
    flagged_seconds = sum(sp["end"] - sp["start"] for sp in flagged_spans)
    reasons_count: dict[str, int] = {}
    for sp in flagged_spans:
        for r in sp["reasons"]:
            reasons_count[r] = reasons_count.get(r, 0) + 1
    return {
        "mean_usable": mean_usable,
        "flagged_seconds": flagged_seconds,
        "flagged_fraction": round(flagged_seconds / duration, 3) if duration else 0.0,
        "reasons_count": reasons_count,
    }


def build_quality_doc(
    duration: float,
    fps_sampled: float,
    samples: list[dict],
    black_spans: list[dict] | None = None,
) -> dict:
    """Assemble the stored quality sidecar document (see module docstring)."""
    buckets = bucket_by_second(samples, duration)
    seconds = score_seconds(buckets)
    if black_spans:
        apply_forced_spans(seconds, black_spans, "black")
    seconds.sort(key=lambda s: s["t"])
    flagged_spans = merge_flagged_spans(seconds)
    summary = summarize(seconds, flagged_spans, duration)
    return {
        "version": QUALITY_VERSION,
        "duration": round(float(duration), 3),
        "fps_sampled": fps_sampled,
        "seconds": seconds,
        "flagged_spans": flagged_spans,
        "summary": summary,
    }


_BLACKDETECT_RE = re.compile(
    r"black_start:\s*([\d.]+)\s+black_end:\s*([\d.]+)"
)


def parse_blackdetect(stderr: str) -> list[dict]:
    """Parse ffmpeg `blackdetect` filter stderr lines into [{start, end}].

    Matches lines like:
      [blackdetect @ 0x...] black_start:12.34 black_end:15.67 black_duration:3.33
    """
    spans = []
    for m in _BLACKDETECT_RE.finditer(stderr or ""):
        start, end = float(m.group(1)), float(m.group(2))
        if end > start:
            spans.append({"start": round(start, 3), "end": round(end, 3)})
    return spans
