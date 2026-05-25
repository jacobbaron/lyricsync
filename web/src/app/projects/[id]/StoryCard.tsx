"use client";

import { useCallback, useId, useState } from "react";
import { useRouter } from "next/navigation";
import type { ClipMeta } from "./RangePicker";

// ── types ─────────────────────────────────────────────────────────────────

interface RangeInput {
  source: string;
  start: string; // mm:ss or decimal string — raw input value
  end: string;
}

interface RangeData {
  source: string;
  start: number;
  end: number;
}

export interface StoryData {
  id: string;
  title: string | null;
  description: string | null;
  estimated_duration_secs: number | null;
  ranges_json: RangeData[] | null;
  status: string;
}

// ── helpers ───────────────────────────────────────────────────────────────

function parseTime(raw: string): number {
  const s = raw.trim();
  if (!s) return NaN;
  const ci = s.lastIndexOf(":");
  if (ci !== -1) {
    const m = parseInt(s.slice(0, ci), 10);
    const sec = parseFloat(s.slice(ci + 1));
    if (isNaN(m) || isNaN(sec) || sec < 0 || sec >= 60) return NaN;
    return m * 60 + sec;
  }
  return parseFloat(s);
}

function secsToInput(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = (secs % 60).toFixed(2).padStart(5, "0");
  return `${m}:${s}`;
}

function fmtDuration(secs: number): string {
  if (secs < 60) return `${Math.round(secs)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

function rangeToInput(r: RangeData): RangeInput {
  return { source: r.source, start: secsToInput(r.start), end: secsToInput(r.end) };
}

// ── sub-component ─────────────────────────────────────────────────────────

function TimeInput({
  id, value, placeholder, onChange, error,
}: {
  id: string; value: string; placeholder: string;
  onChange: (v: string) => void; error?: string;
}) {
  return (
    <div className="flex flex-col gap-0.5 min-w-0">
      <input
        id={id} type="text" inputMode="decimal" value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={[
          "w-full rounded-lg border px-2 py-1.5 text-xs font-mono bg-white dark:bg-zinc-900",
          "text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400",
          "focus:outline-none focus:ring-2 focus:ring-blue-500",
          error
            ? "border-red-400 dark:border-red-500"
            : "border-zinc-200 dark:border-zinc-700",
        ].join(" ")}
      />
      {error && <p className="text-xs text-red-500">{error}</p>}
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────

interface Props {
  story: StoryData;
  clips: ClipMeta[];
  index: number; // 1-based option number within the round
}

export function StoryCard({ story, clips, index }: Props) {
  const router = useRouter();
  const uid = useId();
  const durationMap = Object.fromEntries(
    clips.map((c) => [c.filename, c.duration_secs ?? Infinity]),
  );

  // Initialise ranges from Claude's suggestion
  const [ranges, setRanges] = useState<RangeInput[]>(
    () => (story.ranges_json ?? []).map(rangeToInput),
  );
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Validation
  function validateRange(r: RangeInput): { start?: string; end?: string } {
    const errs: { start?: string; end?: string } = {};
    const startSecs = parseTime(r.start);
    const endSecs = parseTime(r.end);
    const maxDur = durationMap[r.source] ?? Infinity;
    if (r.start && isNaN(startSecs)) errs.start = "Invalid time";
    else if (!isNaN(startSecs) && startSecs < 0) errs.start = "Must be ≥ 0";
    else if (!isNaN(startSecs) && startSecs >= maxDur) errs.start = "Past clip end";
    if (r.end && isNaN(endSecs)) errs.end = "Invalid time";
    else if (!isNaN(endSecs) && !isNaN(startSecs) && endSecs <= startSecs)
      errs.end = "Must be after start";
    else if (!isNaN(endSecs) && endSecs > maxDur) errs.end = "Past clip end";
    return errs;
  }

  const allErrors = ranges.map(validateRange);
  const hasErrors = allErrors.some((e) => Object.keys(e).length > 0);
  const allFilled = ranges.every((r) => r.source && r.start.trim() && r.end.trim());

  const update = useCallback(
    (i: number, patch: Partial<RangeInput>) =>
      setRanges((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r))),
    [],
  );

  const addRange = () =>
    setRanges((prev) => [...prev, { source: clips[0]?.filename ?? "", start: "", end: "" }]);

  const removeRange = (i: number) =>
    setRanges((prev) => prev.filter((_, idx) => idx !== i));

  const moveUp = (i: number) =>
    setRanges((prev) => {
      if (i === 0) return prev;
      const n = [...prev];
      [n[i - 1], n[i]] = [n[i], n[i - 1]];
      return n;
    });

  const moveDown = (i: number) =>
    setRanges((prev) => {
      if (i >= prev.length - 1) return prev;
      const n = [...prev];
      [n[i], n[i + 1]] = [n[i + 1], n[i]];
      return n;
    });

  async function handleRender() {
    if (!allFilled || hasErrors) return;
    setSubmitting(true);
    setSubmitError(null);
    const payload = ranges.map((r) => ({
      source: r.source,
      start: parseTime(r.start),
      end: parseTime(r.end),
    }));
    try {
      const res = await fetch(`/api/stories/${story.id}/render`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ranges: payload }),
      });
      const data = await res.json();
      if (!res.ok) {
        setSubmitError(data.error ?? "Something went wrong");
        return;
      }
      router.push(`/stories/${story.id}`);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Network error");
    } finally {
      setSubmitting(false);
    }
  }

  // Placeholder state while generating
  if (story.status === "generating" || !story.title) {
    return (
      <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-4 flex flex-col gap-3 animate-pulse">
        <div className="flex items-center justify-between">
          <span className="text-xs text-zinc-400 dark:text-zinc-500">
            Option {index}
          </span>
          <span className="text-xs text-blue-500">Generating·</span>
        </div>
        <div className="h-4 w-2/3 rounded-full bg-zinc-100 dark:bg-zinc-800" />
        <div className="h-10 rounded-lg bg-zinc-100 dark:bg-zinc-800" />
      </div>
    );
  }

  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-4 flex flex-col gap-4">
      {/* Header */}
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1 min-w-0">
          <div className="flex items-center gap-2">
            <span className="text-xs text-zinc-400 dark:text-zinc-500 shrink-0">
              Option {index}
            </span>
            {story.estimated_duration_secs != null && (
              <span className="text-xs font-mono text-zinc-400 dark:text-zinc-500">
                {fmtDuration(story.estimated_duration_secs)}
              </span>
            )}
          </div>
          <h3 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
            {story.title}
          </h3>
        </div>
      </div>

      {/* Description */}
      {story.description && (
        <p className="text-xs leading-relaxed text-zinc-500 dark:text-zinc-400">
          {story.description}
        </p>
      )}

      {/* Editable ranges */}
      <div className="flex flex-col gap-2">
        <p className="text-xs font-medium text-zinc-400 dark:text-zinc-500">
          Ranges — edit freely before rendering
        </p>
        {ranges.map((range, i) => {
          const errs = allErrors[i];
          const dur = durationMap[range.source];
          const durLabel = dur && isFinite(dur)
            ? `${Math.floor(dur / 60)}:${(dur % 60).toFixed(2).padStart(5, "0")}`
            : null;
          return (
            <div
              key={i}
              className="rounded-lg border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/50 px-3 py-2 flex flex-col gap-2"
            >
              {/* Range header */}
              <div className="flex items-center justify-between gap-1">
                <span className="text-xs text-zinc-400">#{i + 1}</span>
                <div className="flex items-center gap-1">
                  <button onClick={() => moveUp(i)} disabled={i === 0}
                    className="px-1 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 disabled:opacity-30 text-xs">↑</button>
                  <button onClick={() => moveDown(i)} disabled={i === ranges.length - 1}
                    className="px-1 text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 disabled:opacity-30 text-xs">↓</button>
                  {ranges.length > 1 && (
                    <button onClick={() => removeRange(i)}
                      className="px-1 text-zinc-400 hover:text-red-500 text-xs ml-0.5">✕</button>
                  )}
                </div>
              </div>
              {/* Source */}
              <select
                value={range.source}
                onChange={(e) => update(i, { source: e.target.value })}
                className="w-full rounded border border-zinc-200 dark:border-zinc-700 px-2 py-1 text-xs bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-1 focus:ring-blue-500"
              >
                {clips.map((c) => (
                  <option key={c.id} value={c.filename}>{c.filename}</option>
                ))}
              </select>
              {durLabel && (
                <p className="text-xs text-zinc-400 dark:text-zinc-500 -mt-1">
                  max {durLabel}
                </p>
              )}
              {/* Start / End */}
              <div className="grid grid-cols-2 gap-2">
                <div className="flex flex-col gap-0.5">
                  <label className="text-xs text-zinc-400">Start</label>
                  <TimeInput id={`${uid}-${i}-s`} value={range.start}
                    placeholder="0:12 or 12.5"
                    onChange={(v) => update(i, { start: v })}
                    error={range.start ? errs.start : undefined} />
                </div>
                <div className="flex flex-col gap-0.5">
                  <label className="text-xs text-zinc-400">End</label>
                  <TimeInput id={`${uid}-${i}-e`} value={range.end}
                    placeholder="0:15 or 15.5"
                    onChange={(v) => update(i, { end: v })}
                    error={range.end ? errs.end : undefined} />
                </div>
              </div>
            </div>
          );
        })}

        <button onClick={addRange}
          className="w-full rounded-lg border border-dashed border-zinc-200 dark:border-zinc-700 py-1.5 text-xs text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300 transition-colors">
          + Add range
        </button>
      </div>

      {submitError && <p className="text-xs text-red-500">{submitError}</p>}

      <button
        onClick={handleRender}
        disabled={submitting || !allFilled || hasErrors}
        className="w-full rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed px-4 py-2.5 text-sm font-semibold text-white transition-colors"
      >
        {submitting ? "Starting render…" : "Render this cut"}
      </button>
    </div>
  );
}
