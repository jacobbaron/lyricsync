"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Per-clip audio visualization: a zoomable/pannable canvas timeline showing the
// waveform, the Silero VAD speech-probability curve + speech intervals, and the
// aligned word boxes — with synced audio playback (click to seek, space to
// play). Read-only: this is the "see what's actually there" surface that the
// interactive cut tooling (phase 2) will build on. Data comes from
// GET /api/clips/[id]/audio (built by the Modal analyze_clip_audio worker).

type Word = { text: string; start: number; end: number; score: number | null };
type Interval = { start: number; end: number };
type Analysis = {
  duration: number;
  waveform: { hop: number; peaks: number[] };
  vad: { hop: number; prob: number[] | null; intervals: Interval[] };
  words: Word[];
};
type AudioResponse = { audio_url: string; analysis: Analysis };

const H = 240; // canvas CSS height
const RULER_H = 18;
const WORDS_H = 70;
const VAD_THRESHOLD = 0.5;

function fmt(t: number): string {
  if (!isFinite(t)) return "0:00";
  const m = Math.floor(t / 60);
  const s = Math.floor(t % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

const clamp = (v: number, lo: number, hi: number) =>
  Math.max(lo, Math.min(hi, v));

export function ClipAudioViz({
  clipId,
  filename,
  durationSecs,
}: {
  clipId: string;
  filename: string;
  durationSecs: number | null;
}) {
  const [data, setData] = useState<AudioResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [notReady, setNotReady] = useState(false);
  const [analyzing, setAnalyzing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [playing, setPlaying] = useState(false);
  const [clock, setClock] = useState(0);

  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const fetchAudio = useCallback(async (): Promise<boolean> => {
    const res = await fetch(`/api/clips/${clipId}/audio`);
    if (res.status === 409) {
      setNotReady(true);
      return false;
    }
    if (!res.ok) {
      setError(`Failed to load audio (${res.status})`);
      return false;
    }
    setData((await res.json()) as AudioResponse);
    setNotReady(false);
    setError(null);
    return true;
  }, [clipId]);

  // Initial load.
  useEffect(() => {
    let alive = true;
    (async () => {
      await fetchAudio();
      if (alive) setLoading(false);
    })();
    return () => {
      alive = false;
      if (pollRef.current) clearInterval(pollRef.current);
    };
  }, [fetchAudio]);

  const runAnalysis = useCallback(async () => {
    setAnalyzing(true);
    setError(null);
    const res = await fetch(`/api/clips/${clipId}/audio-analysis`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: "{}",
    });
    if (!res.ok) {
      setAnalyzing(false);
      setError(`Could not start analysis (${res.status})`);
      return;
    }
    // Poll until the worker has written the analysis to R2.
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      const ok = await fetchAudio();
      if (ok) {
        if (pollRef.current) clearInterval(pollRef.current);
        setAnalyzing(false);
      }
    }, 3000);
  }, [clipId, fetchAudio]);

  if (loading) {
    return <p className="text-sm text-zinc-500">Loading…</p>;
  }

  if (notReady || !data) {
    return (
      <div className="flex flex-col gap-3 rounded-xl border border-zinc-200 bg-white px-4 py-6 dark:border-zinc-800 dark:bg-zinc-900">
        <p className="text-sm text-zinc-600 dark:text-zinc-300">
          No audio analysis yet for{" "}
          <span className="font-mono">{filename}</span>. Build the waveform +
          speech (VAD) view
          {durationSecs ? ` (~${fmt(durationSecs)} of audio)` : ""}.
        </p>
        {error && <p className="text-xs text-red-500">{error}</p>}
        <button
          onClick={runAnalysis}
          disabled={analyzing}
          className="self-start rounded-lg bg-blue-600 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
        >
          {analyzing ? "Analyzing… (this can take ~30s)" : "Analyze audio"}
        </button>
      </div>
    );
  }

  return (
    <Viz
      data={data}
      playing={playing}
      setPlaying={setPlaying}
      clock={clock}
      setClock={setClock}
      onReanalyze={runAnalysis}
      analyzing={analyzing}
      error={error}
    />
  );
}

function Viz({
  data,
  playing,
  setPlaying,
  clock,
  setClock,
  onReanalyze,
  analyzing,
  error,
}: {
  data: AudioResponse;
  playing: boolean;
  setPlaying: (p: boolean) => void;
  clock: number;
  setClock: (t: number) => void;
  onReanalyze: () => void;
  analyzing: boolean;
  error: string | null;
}) {
  const { analysis, audio_url } = data;
  const duration = analysis.duration || 1;

  const containerRef = useRef<HTMLDivElement>(null);
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const audioRef = useRef<HTMLAudioElement>(null);

  // View state lives in refs so pan/zoom/playhead redraws don't churn React.
  const pxPerSecRef = useRef(0);
  const minPxRef = useRef(0);
  const offsetRef = useRef(0); // seconds at the left edge
  const widthRef = useRef(0);
  const playheadRef = useRef(0);
  // Imperative handles for the control buttons.
  const apiRef = useRef<{
    zoom: (f: number) => void;
    fit: () => void;
  } | null>(null);

  useEffect(() => {
    const canvas = canvasRef.current;
    const container = containerRef.current;
    if (!canvas || !container) return;
    const ctx = canvas.getContext("2d");
    if (!ctx) return;

    const dark = window.matchMedia?.("(prefers-color-scheme: dark)").matches;
    const COL = dark
      ? {
          bg: "#18181b",
          ruler: "#3f3f46",
          rulerText: "#a1a1aa",
          wave: "#52525b",
          interval: "rgba(59,130,246,0.16)",
          vad: "#3b82f6",
          threshold: "#52525b",
          word: "rgba(34,197,94,0.18)",
          wordBorder: "rgba(34,197,94,0.5)",
          wordText: "#d4d4d8",
          playhead: "#ef4444",
        }
      : {
          bg: "#ffffff",
          ruler: "#e4e4e7",
          rulerText: "#71717a",
          wave: "#a1a1aa",
          interval: "rgba(59,130,246,0.12)",
          vad: "#2563eb",
          threshold: "#d4d4d8",
          word: "rgba(22,163,74,0.12)",
          wordBorder: "rgba(22,163,74,0.45)",
          wordText: "#3f3f46",
          playhead: "#dc2626",
        };

    const timeToX = (t: number) => (t - offsetRef.current) * pxPerSecRef.current;
    const xToTime = (x: number) => offsetRef.current + x / pxPerSecRef.current;

    const clampOffset = () => {
      const visible = widthRef.current / pxPerSecRef.current;
      offsetRef.current = clamp(offsetRef.current, 0, Math.max(0, duration - visible));
    };

    const draw = () => {
      const w = widthRef.current;
      ctx.clearRect(0, 0, w, H);
      ctx.fillStyle = COL.bg;
      ctx.fillRect(0, 0, w, H);

      const t0 = offsetRef.current;
      const t1 = xToTime(w);
      const mainTop = RULER_H;
      const mainBot = H - WORDS_H;
      const mainMid = (mainTop + mainBot) / 2;
      const mainH = mainBot - mainTop;

      // Speech intervals (background bands).
      ctx.fillStyle = COL.interval;
      for (const iv of analysis.vad.intervals) {
        if (iv.end < t0 || iv.start > t1) continue;
        const x = timeToX(iv.start);
        ctx.fillRect(x, mainTop, timeToX(iv.end) - x, mainH);
      }

      // Waveform (mirrored peak bars around the midline).
      const { hop: whop, peaks } = analysis.waveform;
      ctx.strokeStyle = COL.wave;
      ctx.lineWidth = 1;
      ctx.beginPath();
      const i0 = Math.max(0, Math.floor(t0 / whop));
      const i1 = Math.min(peaks.length - 1, Math.ceil(t1 / whop));
      for (let i = i0; i <= i1; i++) {
        const x = Math.round(timeToX(i * whop)) + 0.5;
        const a = (peaks[i] * mainH) / 2;
        ctx.moveTo(x, mainMid - a);
        ctx.lineTo(x, mainMid + a);
      }
      ctx.stroke();

      // VAD threshold guide.
      const vy = (p: number) => mainBot - p * mainH;
      ctx.strokeStyle = COL.threshold;
      ctx.setLineDash([4, 4]);
      ctx.beginPath();
      ctx.moveTo(0, vy(VAD_THRESHOLD));
      ctx.lineTo(w, vy(VAD_THRESHOLD));
      ctx.stroke();
      ctx.setLineDash([]);

      // VAD probability curve (or a step curve from intervals if absent).
      ctx.strokeStyle = COL.vad;
      ctx.lineWidth = 1.5;
      ctx.beginPath();
      const prob = analysis.vad.prob;
      if (prob) {
        const vhop = analysis.vad.hop;
        const j0 = Math.max(0, Math.floor(t0 / vhop));
        const j1 = Math.min(prob.length - 1, Math.ceil(t1 / vhop));
        for (let j = j0; j <= j1; j++) {
          const x = timeToX(j * vhop);
          const y = vy(prob[j]);
          if (j === j0) ctx.moveTo(x, y);
          else ctx.lineTo(x, y);
        }
      } else {
        let started = false;
        const step = (t: number, p: number) => {
          const x = timeToX(t);
          const y = vy(p);
          if (!started) {
            ctx.moveTo(x, y);
            started = true;
          } else ctx.lineTo(x, y);
        };
        let cursor = t0;
        for (const iv of analysis.vad.intervals) {
          if (iv.end < t0 || iv.start > t1) continue;
          step(cursor, 0);
          step(iv.start, 0);
          step(iv.start, 1);
          step(iv.end, 1);
          step(iv.end, 0);
          cursor = iv.end;
        }
      }
      ctx.stroke();

      // Word lane.
      const wTop = mainBot + 6;
      const wBot = H - 4;
      ctx.font = "11px ui-sans-serif, system-ui, sans-serif";
      ctx.textBaseline = "middle";
      for (const word of analysis.words) {
        if (word.end < t0 || word.start > t1) continue;
        const x = timeToX(word.start);
        const ww = Math.max(2, timeToX(word.end) - x);
        ctx.fillStyle = COL.word;
        ctx.fillRect(x, wTop, ww, wBot - wTop);
        ctx.strokeStyle = COL.wordBorder;
        ctx.lineWidth = 1;
        ctx.strokeRect(x + 0.5, wTop + 0.5, ww - 1, wBot - wTop - 1);
        if (ww > 22 && word.text) {
          ctx.save();
          ctx.beginPath();
          ctx.rect(x + 2, wTop, ww - 4, wBot - wTop);
          ctx.clip();
          ctx.fillStyle = COL.wordText;
          ctx.fillText(word.text, x + 4, (wTop + wBot) / 2);
          ctx.restore();
        }
      }

      // Ruler ticks (~every 80px, snapped to a "nice" second step).
      ctx.fillStyle = COL.ruler;
      ctx.fillRect(0, 0, w, RULER_H);
      const targetPx = 80;
      const rawStep = targetPx / pxPerSecRef.current;
      const niceSteps = [0.5, 1, 2, 5, 10, 15, 30, 60, 120];
      const step = niceSteps.find((s) => s >= rawStep) ?? 120;
      ctx.fillStyle = COL.rulerText;
      ctx.strokeStyle = COL.ruler;
      ctx.font = "10px ui-sans-serif, system-ui, sans-serif";
      ctx.textBaseline = "top";
      for (let t = Math.ceil(t0 / step) * step; t <= t1; t += step) {
        const x = timeToX(t);
        ctx.beginPath();
        ctx.moveTo(x + 0.5, RULER_H - 5);
        ctx.lineTo(x + 0.5, RULER_H);
        ctx.stroke();
        ctx.fillText(fmt(t), x + 3, 3);
      }

      // Playhead.
      const px = timeToX(playheadRef.current);
      if (px >= 0 && px <= w) {
        ctx.strokeStyle = COL.playhead;
        ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(px + 0.5, 0);
        ctx.lineTo(px + 0.5, H);
        ctx.stroke();
      }
    };

    // Sizing (DPR-aware).
    const resize = () => {
      const cssW = container.clientWidth;
      widthRef.current = cssW;
      const dpr = window.devicePixelRatio || 1;
      canvas.width = Math.round(cssW * dpr);
      canvas.height = Math.round(H * dpr);
      canvas.style.width = `${cssW}px`;
      canvas.style.height = `${H}px`;
      ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
      minPxRef.current = cssW / duration;
      if (pxPerSecRef.current < minPxRef.current) pxPerSecRef.current = minPxRef.current;
      clampOffset();
      draw();
    };

    // Fit the whole clip.
    pxPerSecRef.current = 0; // force resize() to snap to fit
    const ro = new ResizeObserver(resize);
    ro.observe(container);
    resize();

    // Zoom around a focal x.
    const zoomAt = (focalX: number, factor: number) => {
      const tFocal = xToTime(focalX);
      pxPerSecRef.current = clamp(
        pxPerSecRef.current * factor,
        minPxRef.current,
        600,
      );
      offsetRef.current = tFocal - focalX / pxPerSecRef.current;
      clampOffset();
      draw();
    };

    apiRef.current = {
      zoom: (f) => zoomAt(widthRef.current / 2, f),
      fit: () => {
        pxPerSecRef.current = minPxRef.current;
        offsetRef.current = 0;
        draw();
      },
    };

    const onWheel = (e: WheelEvent) => {
      e.preventDefault();
      const rect = canvas.getBoundingClientRect();
      if (e.ctrlKey || Math.abs(e.deltaY) >= Math.abs(e.deltaX)) {
        zoomAt(e.clientX - rect.left, Math.exp(-e.deltaY * 0.0015));
      } else {
        offsetRef.current += e.deltaX / pxPerSecRef.current;
        clampOffset();
        draw();
      }
    };

    // Pointer: drag to pan, click (no drag) to seek.
    let down = false;
    let moved = 0;
    let startX = 0;
    let startOffset = 0;
    const onDown = (e: PointerEvent) => {
      down = true;
      moved = 0;
      startX = e.clientX;
      startOffset = offsetRef.current;
      canvas.setPointerCapture(e.pointerId);
    };
    const onMove = (e: PointerEvent) => {
      if (!down) return;
      const dx = e.clientX - startX;
      moved = Math.max(moved, Math.abs(dx));
      offsetRef.current = startOffset - dx / pxPerSecRef.current;
      clampOffset();
      draw();
    };
    const onUp = (e: PointerEvent) => {
      if (down && moved < 4) {
        const rect = canvas.getBoundingClientRect();
        const t = clamp(xToTime(e.clientX - rect.left), 0, duration);
        playheadRef.current = t;
        if (audioRef.current) audioRef.current.currentTime = t;
        draw();
      }
      down = false;
    };

    canvas.addEventListener("wheel", onWheel, { passive: false });
    canvas.addEventListener("pointerdown", onDown);
    canvas.addEventListener("pointermove", onMove);
    canvas.addEventListener("pointerup", onUp);

    // Playhead animation loop (auto-scrolls to keep it on screen).
    let raf = 0;
    const tick = () => {
      const audio = audioRef.current;
      if (audio && !audio.paused) {
        playheadRef.current = audio.currentTime;
        const px = timeToX(audio.currentTime);
        if (px < 0 || px > widthRef.current * 0.9) {
          offsetRef.current = audio.currentTime - (widthRef.current * 0.15) / pxPerSecRef.current;
          clampOffset();
        }
        draw();
      }
      raf = requestAnimationFrame(tick);
    };
    raf = requestAnimationFrame(tick);

    return () => {
      ro.disconnect();
      cancelAnimationFrame(raf);
      canvas.removeEventListener("wheel", onWheel);
      canvas.removeEventListener("pointerdown", onDown);
      canvas.removeEventListener("pointermove", onMove);
      canvas.removeEventListener("pointerup", onUp);
      apiRef.current = null;
    };
  }, [analysis, duration]);

  // Keyboard: space toggles playback.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.code === "Space" && e.target === document.body) {
        e.preventDefault();
        const a = audioRef.current;
        if (!a) return;
        if (a.paused) a.play();
        else a.pause();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  const togglePlay = () => {
    const a = audioRef.current;
    if (!a) return;
    if (a.paused) a.play();
    else a.pause();
  };

  const hasProb = analysis.vad.prob != null;

  return (
    <div className="flex flex-col gap-3">
      <div
        ref={containerRef}
        className="w-full overflow-hidden rounded-xl border border-zinc-200 dark:border-zinc-800"
        style={{ height: H }}
      >
        <canvas ref={canvasRef} className="block touch-none" />
      </div>

      <audio
        ref={audioRef}
        src={audio_url}
        preload="auto"
        onPlay={() => setPlaying(true)}
        onPause={() => setPlaying(false)}
        onTimeUpdate={(e) => setClock(e.currentTarget.currentTime)}
      />

      <div className="flex flex-wrap items-center gap-2 text-sm">
        <button
          onClick={togglePlay}
          className="rounded-lg bg-blue-600 px-3 py-1.5 font-semibold text-white"
        >
          {playing ? "❚❚ Pause" : "▶ Play"}
        </button>
        <span className="font-mono text-xs text-zinc-500">
          {fmt(clock)} / {fmt(duration)}
        </span>
        <span className="mx-1 h-4 w-px bg-zinc-300 dark:bg-zinc-700" />
        <button
          onClick={() => apiRef.current?.zoom(1.6)}
          className="rounded-lg border border-zinc-300 px-2.5 py-1.5 dark:border-zinc-700"
          aria-label="Zoom in"
        >
          ＋
        </button>
        <button
          onClick={() => apiRef.current?.zoom(1 / 1.6)}
          className="rounded-lg border border-zinc-300 px-2.5 py-1.5 dark:border-zinc-700"
          aria-label="Zoom out"
        >
          －
        </button>
        <button
          onClick={() => apiRef.current?.fit()}
          className="rounded-lg border border-zinc-300 px-2.5 py-1.5 dark:border-zinc-700"
        >
          Fit
        </button>
        <span className="ml-auto">
          <button
            onClick={onReanalyze}
            disabled={analyzing}
            className="rounded-lg border border-zinc-300 px-2.5 py-1.5 text-xs text-zinc-500 disabled:opacity-50 dark:border-zinc-700"
          >
            {analyzing ? "Re-analyzing…" : "Re-analyze"}
          </button>
        </span>
      </div>

      <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-zinc-500">
        <span>
          <span className="inline-block h-2 w-3 align-middle" style={{ background: "#a1a1aa" }} />{" "}
          waveform
        </span>
        <span>
          <span className="inline-block h-2 w-3 align-middle" style={{ background: "#2563eb" }} />{" "}
          {hasProb ? "VAD speech probability" : "speech (energy-gated)"}
        </span>
        <span>
          <span className="inline-block h-2 w-3 align-middle" style={{ background: "rgba(22,163,74,0.45)" }} />{" "}
          words ({analysis.words.length})
        </span>
        <span className="text-zinc-400">scroll = zoom · drag = pan · click = seek · space = play</span>
      </div>
      {error && <p className="text-xs text-red-500">{error}</p>}
    </div>
  );
}
