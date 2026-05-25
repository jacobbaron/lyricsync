"use client";

import { useRouter } from "next/navigation";
import { useCallback, useId, useState } from "react";

// ── types ─────────────────────────────────────────────────────────────────

export interface ClipMeta {
  id: string;
  filename: string;
  duration_secs: number | null;
}

interface Range {
  source: string; // matches clip.filename
  start: string; // raw user input (mm:ss or decimal)
  end: string;
}

// ── helpers ───────────────────────────────────────────────────────────────

/**
 * Parse a time string to seconds.
 * Accepts: "1:30", "1:30.5", "90", "90.5"
 * Returns NaN on invalid input.
 */
function parseTime(raw: string): number {
  const s = raw.trim();
  if (!s) return NaN;
  const colonIdx = s.lastIndexOf(":");
  if (colonIdx !== -1) {
    const m = parseInt(s.slice(0, colonIdx), 10);
    const sec = parseFloat(s.slice(colonIdx + 1));
    if (isNaN(m) || isNaN(sec) || sec < 0 || sec >= 60) return NaN;
    return m * 60 + sec;
  }
  return parseFloat(s);
}

function fmtSecs(s: number): string {
  const m = Math.floor(s / 60);
  const sec = (s % 60).toFixed(2).padStart(5, "0");
  return `${m}:${sec}`;
}

function makeRange(clips: ClipMeta[]): Range {
  return { source: clips[0]?.filename ?? "", start: "", end: "" };
}

// ── sub-components ────────────────────────────────────────────────────────

function TimeInput({
  id,
  value,
  placeholder,
  onChange,
  error,
}: {
  id: string;
  value: string;
  placeholder: string;
  onChange: (v: string) => void;
  error?: string;
}) {
  return (
    <div className="flex flex-col gap-1 min-w-0">
      <input
        id={id}
        type="text"
        inputMode="decimal"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        className={[
          "w-full rounded-lg border px-3 py-2 text-sm font-mono bg-white dark:bg-zinc-900",
          "text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400",
          "focus:outline-none focus:ring-2 focus:ring-blue-500",
          error
            ? "border-red-400 dark:border-red-500"
            : "border-zinc-200 dark:border-zinc-700",
        ].join(" ")}
      />
      {error && (
        <p className="text-xs text-red-500">{error}</p>
      )}
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────

interface Props {
  projectId: string;
  clips: ClipMeta[];
}

export function RangePicker({ projectId, clips }: Props) {
  const router = useRouter();
  const uid = useId();
  const [ranges, setRanges] = useState<Range[]>([makeRange(clips)]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState<string | null>(null);

  // Build duration lookup
  const durationMap = Object.fromEntries(
    clips.map((c) => [c.filename, c.duration_secs ?? Infinity]),
  );

  // Per-range validation
  function validateRange(r: Range): { start?: string; end?: string } {
    const errs: { start?: string; end?: string } = {};
    const startSecs = parseTime(r.start);
    const endSecs = parseTime(r.end);
    const maxDur = durationMap[r.source] ?? Infinity;

    if (r.start && isNaN(startSecs)) {
      errs.start = "Invalid time (use mm:ss or seconds)";
    } else if (!isNaN(startSecs) && startSecs < 0) {
      errs.start = "Must be ≥ 0";
    } else if (!isNaN(startSecs) && startSecs >= maxDur) {
      errs.start = `Must be < ${fmtSecs(maxDur)}`;
    }

    if (r.end && isNaN(endSecs)) {
      errs.end = "Invalid time (use mm:ss or seconds)";
    } else if (!isNaN(endSecs) && !isNaN(startSecs) && endSecs <= startSecs) {
      errs.end = "Must be after start";
    } else if (!isNaN(endSecs) && endSecs > maxDur) {
      errs.end = `Must be ≤ ${fmtSecs(maxDur)}`;
    }

    return errs;
  }

  const allErrors = ranges.map(validateRange);
  const hasErrors = allErrors.some((e) => Object.keys(e).length > 0);
  const allFilled = ranges.every(
    (r) => r.source && r.start.trim() && r.end.trim(),
  );

  // Mutation helpers
  const update = useCallback(
    (i: number, patch: Partial<Range>) =>
      setRanges((prev) =>
        prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)),
      ),
    [],
  );

  const addRange = () => setRanges((prev) => [...prev, makeRange(clips)]);

  const removeRange = (i: number) =>
    setRanges((prev) => prev.filter((_, idx) => idx !== i));

  const moveUp = (i: number) => {
    if (i === 0) return;
    setRanges((prev) => {
      const next = [...prev];
      [next[i - 1], next[i]] = [next[i], next[i - 1]];
      return next;
    });
  };

  const moveDown = (i: number) => {
    setRanges((prev) => {
      if (i >= prev.length - 1) return prev;
      const next = [...prev];
      [next[i], next[i + 1]] = [next[i + 1], next[i]];
      return next;
    });
  };

  async function handleSubmit() {
    if (!allFilled || hasErrors) return;
    setSubmitting(true);
    setSubmitError(null);

    const payload = ranges.map((r) => ({
      source: r.source,
      start: parseTime(r.start),
      end: parseTime(r.end),
    }));

    try {
      const res = await fetch(`/api/projects/${projectId}/stories`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ ranges: payload }),
      });
      const data = await res.json();
      if (!res.ok) {
        setSubmitError(data.error ?? "Something went wrong");
        return;
      }
      router.push(`/stories/${data.id}`);
    } catch (err) {
      setSubmitError(err instanceof Error ? err.message : "Network error");
    } finally {
      setSubmitting(false);
    }
  }

  return (
    <section className="flex flex-col gap-4">
      <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
        Pick Ranges
      </h2>

      <ul className="flex flex-col gap-3">
        {ranges.map((range, i) => {
          const errs = allErrors[i];
          const rangeId = `${uid}-range-${i}`;
          const dur = durationMap[range.source];
          const durLabel =
            dur && isFinite(dur) ? `max ${fmtSecs(dur)}` : null;

          return (
            <li
              key={i}
              className="rounded-xl border border-zinc-200 bg-white px-4 py-4 dark:border-zinc-800 dark:bg-zinc-900 flex flex-col gap-3"
            >
              {/* Header row: index + reorder + remove */}
              <div className="flex items-center justify-between gap-2">
                <span className="text-xs font-medium text-zinc-400 dark:text-zinc-500">
                  Range {i + 1}
                </span>
                <div className="flex items-center gap-1">
                  <button
                    onClick={() => moveUp(i)}
                    disabled={i === 0}
                    title="Move up"
                    className="p-1 rounded text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200 disabled:opacity-30 transition-colors"
                  >
                    ↑
                  </button>
                  <button
                    onClick={() => moveDown(i)}
                    disabled={i === ranges.length - 1}
                    title="Move down"
                    className="p-1 rounded text-zinc-400 hover:text-zinc-700 dark:hover:text-zinc-200 disabled:opacity-30 transition-colors"
                  >
                    ↓
                  </button>
                  {ranges.length > 1 && (
                    <button
                      onClick={() => removeRange(i)}
                      title="Remove range"
                      className="p-1 rounded text-zinc-400 hover:text-red-500 transition-colors ml-1"
                    >
                      ✕
                    </button>
                  )}
                </div>
              </div>

              {/* Source clip */}
              <div className="flex flex-col gap-1">
                <label
                  htmlFor={`${rangeId}-src`}
                  className="text-xs text-zinc-500 dark:text-zinc-400"
                >
                  Source clip
                </label>
                <select
                  id={`${rangeId}-src`}
                  value={range.source}
                  onChange={(e) => update(i, { source: e.target.value })}
                  className="w-full rounded-lg border border-zinc-200 dark:border-zinc-700 px-3 py-2 text-sm bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
                >
                  {clips.map((c) => (
                    <option key={c.id} value={c.filename}>
                      {c.filename}
                    </option>
                  ))}
                </select>
                {durLabel && (
                  <p className="text-xs text-zinc-400 dark:text-zinc-500">
                    Duration: {durLabel}
                  </p>
                )}
              </div>

              {/* Start / End */}
              <div className="grid grid-cols-2 gap-3">
                <div className="flex flex-col gap-1">
                  <label
                    htmlFor={`${rangeId}-start`}
                    className="text-xs text-zinc-500 dark:text-zinc-400"
                  >
                    Start
                  </label>
                  <TimeInput
                    id={`${rangeId}-start`}
                    value={range.start}
                    placeholder="0:12 or 12.5"
                    onChange={(v) => update(i, { start: v })}
                    error={range.start ? errs.start : undefined}
                  />
                </div>
                <div className="flex flex-col gap-1">
                  <label
                    htmlFor={`${rangeId}-end`}
                    className="text-xs text-zinc-500 dark:text-zinc-400"
                  >
                    End
                  </label>
                  <TimeInput
                    id={`${rangeId}-end`}
                    value={range.end}
                    placeholder="0:15 or 15.5"
                    onChange={(v) => update(i, { end: v })}
                    error={range.end ? errs.end : undefined}
                  />
                </div>
              </div>
            </li>
          );
        })}
      </ul>

      {/* Add range */}
      <button
        onClick={addRange}
        className="w-full rounded-xl border border-dashed border-zinc-300 dark:border-zinc-700 py-3 text-sm text-zinc-500 dark:text-zinc-400 hover:border-zinc-400 dark:hover:border-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors"
      >
        + Add range
      </button>

      {/* Submit */}
      {submitError && (
        <p className="text-sm text-red-500 px-1">{submitError}</p>
      )}
      <button
        onClick={handleSubmit}
        disabled={submitting || !allFilled || hasErrors}
        className="w-full rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-40 disabled:cursor-not-allowed px-4 py-3 text-sm font-semibold text-white transition-colors"
      >
        {submitting ? "Creating cut…" : "Create Cut"}
      </button>
    </section>
  );
}
