"""Pure audio-analysis helpers for the clip waveform / VAD visualization.

No Modal/ffmpeg/numpy/network deps — stdlib only — so it imports cleanly in
tests (mirrors transcript.py / timeline.py). The fragile parts (ffmpeg decode,
Silero ONNX inference) live in app.py and feed their raw outputs through the
pure functions here, which do the data shaping the frontend consumes.

Stored analysis JSON shape (R2: projects/<pid>/clips/<cid>/audio_analysis.json):

    {
      "version": 1,
      "duration": 56.82,            # seconds
      "audio_key": "projects/.../audio.m4a",
      "waveform": {"hop": 0.02, "peaks": [0.0..1.0, ...]},
      "vad": {"hop": 0.032,
              "prob": [0.0..1.0, ...] | null,   # per-window speech probability
              "intervals": [{"start", "end"}, ...]},
      "words": [{"text", "start", "end", "score"}, ...]
    }

`prob` is null when the VAD model couldn't run; the frontend then falls back to
drawing a step curve from `intervals` (which are derived from the same probs,
or from a coarse energy gate as a last resort).
"""

from __future__ import annotations

ANALYSIS_VERSION = 1


def downsample(values: list[float], factor: int) -> list[float]:
    """Average-pool `values` by an integer factor (peak/curve decimation).

    Returns the input unchanged for factor <= 1. The final partial bin is
    averaged over whatever samples remain.
    """
    if factor <= 1 or not values:
        return [round(float(v), 4) for v in values]
    out: list[float] = []
    for i in range(0, len(values), factor):
        chunk = values[i:i + factor]
        out.append(round(sum(chunk) / len(chunk), 4))
    return out


def intervals_from_curve(
    prob: list[float],
    hop: float,
    threshold: float = 0.5,
    neg_threshold: float | None = None,
    min_speech: float = 0.10,
    min_silence: float = 0.10,
    pad: float = 0.0,
    total: float | None = None,
) -> list[dict]:
    """Turn a per-window speech-probability curve into speech intervals.

    Hysteresis gate (Silero-style): speech opens when prob crosses `threshold`
    and only closes after prob stays below `neg_threshold` for `min_silence`
    seconds, so brief dips inside a word don't split it. Runs shorter than
    `min_speech` are dropped. Each kept interval is padded by `pad` seconds and
    overlapping results are merged. Times are clamped to [0, total] when given.
    """
    if not prob:
        return []
    if neg_threshold is None:
        neg_threshold = max(0.0, threshold - 0.15)

    n = len(prob)
    min_sil_win = max(1, round(min_silence / hop)) if hop > 0 else 1
    raw: list[tuple[int, int]] = []
    triggered = False
    start = 0
    temp_end = 0
    for i, p in enumerate(prob):
        if p >= threshold:
            temp_end = 0
            if not triggered:
                triggered = True
                start = i
        elif triggered and p < neg_threshold:
            if not temp_end:
                temp_end = i
            if i - temp_end >= min_sil_win:
                raw.append((start, temp_end))
                triggered = False
                temp_end = 0
    if triggered:
        raw.append((start, n))

    out: list[dict] = []
    for s, e in raw:
        if (e - s) * hop < min_speech:
            continue
        st = max(0.0, s * hop - pad)
        en = e * hop + pad
        if total is not None:
            en = min(en, total)
        if en > st:
            out.append({"start": round(st, 3), "end": round(en, 3)})

    merged: list[dict] = []
    for iv in out:
        if merged and iv["start"] <= merged[-1]["end"]:
            merged[-1]["end"] = max(merged[-1]["end"], iv["end"])
        else:
            merged.append(dict(iv))
    return merged


def words_in_clip(words: list[dict], source: str) -> list[dict]:
    """Filter merged-transcript words to one clip and project to display shape.

    Uses clip-local times (local_start/local_end), so they line up with the
    clip's own audio/VAD timeline. Words missing timing are skipped.
    """
    out: list[dict] = []
    for w in words:
        if source is not None and w.get("source") != source:
            continue
        s = w.get("local_start", w.get("start"))
        e = w.get("local_end", w.get("end"))
        if s is None or e is None:
            continue
        out.append({
            "text": (w.get("text") or w.get("word") or "").strip(),
            "start": round(float(s), 3),
            "end": round(float(e), 3),
            "score": w.get("score"),
        })
    out.sort(key=lambda x: x["start"])
    return out


def build_analysis(
    duration: float,
    audio_key: str,
    waveform_hop: float,
    peaks: list[float],
    vad_hop: float,
    vad_prob: list[float] | None,
    intervals: list[dict],
    words: list[dict],
) -> dict:
    """Assemble the stored analysis document (see module docstring)."""
    return {
        "version": ANALYSIS_VERSION,
        "duration": round(float(duration), 3),
        "audio_key": audio_key,
        "waveform": {"hop": round(float(waveform_hop), 4), "peaks": peaks},
        "vad": {
            "hop": round(float(vad_hop), 4),
            "prob": vad_prob,
            "intervals": intervals,
        },
        "words": words,
    }
