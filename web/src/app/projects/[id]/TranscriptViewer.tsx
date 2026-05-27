"use client";

import { useEffect, useState, useCallback } from "react";

// ── types ─────────────────────────────────────────────────────────────────

interface Word {
  text: string;
  global_start: number;
  global_end: number;
  local_start: number;
  source: string;
  recorded_at?: string | null;
}

interface Utterance {
  source: string;
  global_start: number;
  global_end: number;
  text: string;
  recorded_at: string | null; // recording anchor of the source clip
}

// ── helpers ───────────────────────────────────────────────────────────────

const GAP_THRESHOLD_S = 1.5;

/** Group flat word list into per-source utterances, sorted by global start. */
function buildUtterances(words: Word[]): Utterance[] {
  // Bucket words by source
  const bySource: Record<string, Word[]> = {};
  for (const w of words) {
    (bySource[w.source] ??= []).push(w);
  }

  const utterances: Utterance[] = [];

  for (const [source, ws] of Object.entries(bySource)) {
    ws.sort((a, b) => a.global_start - b.global_start);

    // recorded_at is constant per source — capture it once.
    const recordedAt = ws[0].recorded_at ?? null;
    let curStart = ws[0].global_start;
    let curEnd = ws[0].global_end;
    let curWords: string[] = [];

    for (const w of ws) {
      const gap = w.global_start - curEnd;
      if (curWords.length > 0 && gap > GAP_THRESHOLD_S) {
        utterances.push({ source, global_start: curStart, global_end: curEnd, text: curWords.join(" "), recorded_at: recordedAt });
        curStart = w.global_start;
        curWords = [];
      }
      curWords.push(w.text.trim());
      curEnd = w.global_end;
    }
    if (curWords.length > 0) {
      utterances.push({ source, global_start: curStart, global_end: curEnd, text: curWords.join(" "), recorded_at: recordedAt });
    }
  }

  return utterances.sort((a, b) => a.global_start - b.global_start);
}

function fmtTs(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
}

/**
 * Format an offset within a clip as an absolute wall-clock date + time in the
 * viewer's local timezone (e.g. "May 20, 2:34:07 PM"). `offsetSecs` is the
 * word's local offset (== global offset in the no-offset merge). Falls back to
 * the relative M:SS form when there's no recording anchor or it won't parse.
 */
function fmtClock(recordedAt: string | null, offsetSecs: number): string {
  if (!recordedAt) return fmtTs(offsetSecs);
  const base = new Date(recordedAt).getTime();
  if (Number.isNaN(base)) return fmtTs(offsetSecs);
  return new Date(base + offsetSecs * 1000).toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
    second: "2-digit",
    hour12: true,
  });
}

function shortName(filename: string): string {
  // Strip extension for display — "IMG_2418.MOV" → "IMG_2418"
  return filename.replace(/\.[^.]+$/, "");
}

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  projectId: string;
}

export function TranscriptViewer({ projectId }: Props) {
  const [utterances, setUtterances] = useState<Utterance[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [open, setOpen] = useState(false);
  // copied key: `${utteranceIndex}:start` or `${utteranceIndex}:end`
  const [copied, setCopied] = useState<string | null>(null);

  useEffect(() => {
    fetch(`/api/projects/${projectId}/transcript`)
      .then((r) => r.json())
      .then((data) => {
        if (data.error) throw new Error(data.error);
        setUtterances(buildUtterances(data.words ?? []));
      })
      .catch((err) => setError(err.message));
  }, [projectId]);

  const copyTs = useCallback((value: string, key: string) => {
    navigator.clipboard.writeText(value).then(() => {
      setCopied(key);
      setTimeout(() => setCopied((c) => (c === key ? null : c)), 1500);
    });
  }, []);

  if (error) {
    return (
      <p className="text-sm text-red-500 px-1">
        Could not load transcript: {error}
      </p>
    );
  }

  if (!utterances) {
    return (
      <div className="flex flex-col gap-2 animate-pulse">
        {[80, 60, 90, 50, 70].map((w, i) => (
          <div
            key={i}
            className="h-14 rounded-xl bg-zinc-100 dark:bg-zinc-800"
            style={{ width: `${w}%` }}
          />
        ))}
      </div>
    );
  }

  if (utterances.length === 0) {
    return (
      <p className="text-sm text-zinc-500 dark:text-zinc-400 px-1">
        No words found in transcript.
      </p>
    );
  }

  return (
    <section>
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 mb-3 group"
      >
        <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors">
          Transcript
        </h2>
        <span className="text-xs text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors">
          · {utterances.length}
        </span>
        <span className="ml-auto text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors text-xs">
          {open ? "▾" : "▸"}
        </span>
      </button>
      {open && <ul className="flex flex-col gap-2">
        {utterances.map((u, i) => {
          const startTs = fmtClock(u.recorded_at, u.global_start);
          const endTs = fmtClock(u.recorded_at, u.global_end);
          const startKey = `${i}:start`;
          const endKey = `${i}:end`;
          return (
            <li
              key={i}
              className="rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
            >
              {/* Source + start – end timestamp row */}
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className="text-xs font-medium text-zinc-400 dark:text-zinc-500 truncate">
                  {shortName(u.source)}
                </span>
                <span className="shrink-0 flex items-center gap-1 font-mono text-xs text-zinc-400 dark:text-zinc-500">
                  <button
                    onClick={() => copyTs(startTs, startKey)}
                    title="Copy start time"
                    className="hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors"
                  >
                    {copied === startKey ? "✓" : startTs}
                  </button>
                  <span className="text-zinc-300 dark:text-zinc-600">–</span>
                  <button
                    onClick={() => copyTs(endTs, endKey)}
                    title="Copy end time"
                    className="hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors"
                  >
                    {copied === endKey ? "✓" : endTs}
                  </button>
                </span>
              </div>
              {/* Utterance text */}
              <p className="text-sm leading-snug text-zinc-800 dark:text-zinc-200">
                {u.text}
              </p>
            </li>
          );
        })}
      </ul>}
    </section>
  );
}
