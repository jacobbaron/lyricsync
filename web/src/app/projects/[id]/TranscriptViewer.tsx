"use client";

import { useEffect, useState, useCallback } from "react";

// ── types ─────────────────────────────────────────────────────────────────

interface Word {
  text: string;
  global_start: number;
  global_end: number;
  local_start: number;
  source: string;
}

interface Utterance {
  source: string;
  global_start: number;
  global_end: number;
  text: string;
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

    let curStart = ws[0].global_start;
    let curEnd = ws[0].global_end;
    let curWords: string[] = [];

    for (const w of ws) {
      const gap = w.global_start - curEnd;
      if (curWords.length > 0 && gap > GAP_THRESHOLD_S) {
        utterances.push({ source, global_start: curStart, global_end: curEnd, text: curWords.join(" ") });
        curStart = w.global_start;
        curWords = [];
      }
      curWords.push(w.text.trim());
      curEnd = w.global_end;
    }
    if (curWords.length > 0) {
      utterances.push({ source, global_start: curStart, global_end: curEnd, text: curWords.join(" ") });
    }
  }

  return utterances.sort((a, b) => a.global_start - b.global_start);
}

function fmtTs(secs: number): string {
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60);
  return `${m}:${s.toString().padStart(2, "0")}`;
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

  const copyTs = useCallback((ts: string) => {
    navigator.clipboard.writeText(ts).then(() => {
      setCopied(ts);
      setTimeout(() => setCopied((c) => (c === ts ? null : c)), 1500);
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
      <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 mb-3">
        Transcript
      </h2>
      <ul className="flex flex-col gap-2">
        {utterances.map((u, i) => {
          const ts = fmtTs(u.global_start);
          const isCopied = copied === ts + u.source;
          return (
            <li
              key={i}
              className="rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
            >
              {/* Source + timestamp row */}
              <div className="flex items-center justify-between gap-2 mb-1">
                <span className="text-xs font-medium text-zinc-400 dark:text-zinc-500 truncate">
                  {shortName(u.source)}
                </span>
                <button
                  onClick={() => copyTs(ts + u.source)}
                  title="Copy timestamp"
                  className="shrink-0 text-xs font-mono text-zinc-400 dark:text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors"
                >
                  {isCopied ? "✓ copied" : ts}
                </button>
              </div>
              {/* Utterance text */}
              <p className="text-sm leading-snug text-zinc-800 dark:text-zinc-200">
                {u.text}
              </p>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
