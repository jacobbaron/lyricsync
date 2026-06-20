"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// ── types ──────────────────────────────────────────────────────────────────

interface RenderItem {
  id: string;
  title: string | null;
  status: string;
  error_message: string | null;
  duration_secs: number | null;
  source_count: number;
  created_at: string;
  revision: number;
  updated_at: string;
}

// ── constants ──────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 3000;
const NON_TERMINAL = ["generating", "rendering"];

// ── helpers ─────────────────────────────────────────────────────────────────

function fmtDuration(secs: number | null): string | null {
  if (secs == null) return null;
  if (secs < 60) return `${Math.round(secs)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  return s === 0 ? `${m}m` : `${m}m ${s}s`;
}

function fmtDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleString(undefined, {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function statusBadge(status: string): { label: string; color: string } {
  switch (status) {
    case "done":
      return { label: "Ready", color: "text-green-600 dark:text-green-400" };
    case "rendering":
    case "generating":
      return { label: "Rendering·", color: "text-blue-500" };
    case "error":
      return { label: "Failed", color: "text-red-500" };
    default:
      return { label: status, color: "text-zinc-500 dark:text-zinc-400" };
  }
}

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  projectId: string;
}

/**
 * Lists "direct" renders — cuts created outside the story-generation flow
 * (e.g. through the REST API). These don't belong to a generation round, so
 * they never appear under "Story Options". Each links to the story page where
 * the existing player + download buttons live.
 */
export function DirectRenders({ projectId }: Props) {
  const [renders, setRenders] = useState<RenderItem[]>([]);
  const [loaded, setLoaded] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const hasActive = renders.some((r) => NON_TERMINAL.includes(r.status));

  const fetchRenders = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/renders`);
      if (res.ok) {
        const data = await res.json();
        setRenders(data.renders ?? []);
      }
    } catch {
      // network blip — keep going
    } finally {
      setLoaded(true);
    }
  }, [projectId]);

  useEffect(() => {
    fetchRenders();
  }, [fetchRenders]);

  // Poll while any render is still in flight so "Rendering" flips to "Ready".
  useEffect(() => {
    if (!hasActive) {
      if (pollingRef.current) clearInterval(pollingRef.current);
      return;
    }
    pollingRef.current = setInterval(fetchRenders, POLL_INTERVAL_MS);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [hasActive, fetchRenders]);

  // Nothing to show until we know there's at least one direct render.
  if (!loaded || renders.length === 0) return null;

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
        Rendered Cuts
      </h2>

      <ul className="flex flex-col gap-2">
        {renders.map((r) => {
          const badge = statusBadge(r.status);
          const dur = fmtDuration(r.duration_secs);
          const title = r.title?.trim() || "Untitled cut";
          return (
            <li key={r.id}>
              <a
                href={`/stories/${r.id}`}
                className="flex items-center gap-3 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800/60 transition-colors"
              >
                <div className="flex flex-col gap-1 min-w-0 flex-1">
                  <span className="text-sm font-semibold text-zinc-900 dark:text-zinc-100 truncate">
                    {title}
                  </span>
                  <span className="text-xs text-zinc-400 dark:text-zinc-500 flex items-center gap-2 flex-wrap">
                    <span title={`Last modified ${fmtDate(r.updated_at)}`}>
                      edited {fmtDate(r.updated_at)}
                    </span>
                    {dur && <span className="font-mono">· {dur}</span>}
                    <span>
                      · {r.source_count} {r.source_count === 1 ? "cut" : "cuts"}
                    </span>
                    {r.revision > 0 && (
                      <span className="font-mono">· rev {r.revision}</span>
                    )}
                  </span>
                  {r.status === "error" && r.error_message && (
                    <span className="text-xs text-red-500 truncate">
                      {r.error_message}
                    </span>
                  )}
                </div>
                <span
                  className={`shrink-0 text-xs font-medium ${badge.color}`}
                >
                  {badge.label}
                </span>
                <span className="shrink-0 text-zinc-300 dark:text-zinc-600 text-sm">
                  →
                </span>
              </a>
            </li>
          );
        })}
      </ul>
    </section>
  );
}
