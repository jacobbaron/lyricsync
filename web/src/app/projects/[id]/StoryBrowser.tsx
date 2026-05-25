"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { StoryRound, type RoundData } from "./StoryRound";
import type { ClipMeta } from "./RangePicker";

// ── constants ──────────────────────────────────────────────────────────────

const POLL_INTERVAL_MS = 3000;

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  projectId: string;
  clips: ClipMeta[];
  /** True while project status === "generating_stories" */
  isGenerating: boolean;
  /** Called after user fires a new generation round */
  onGenerated: () => void;
}

export function StoryBrowser({
  projectId,
  clips,
  isGenerating,
  onGenerated,
}: Props) {
  const [rounds, setRounds] = useState<RoundData[]>([]);
  const [prompt, setPrompt] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const prevIsGenerating = useRef(isGenerating);

  const fetchRounds = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/stories`);
      if (res.ok) {
        const data = await res.json();
        setRounds(data.rounds ?? []);
      }
    } catch {
      // network blip — keep going
    }
  }, [projectId]);

  // Initial fetch on mount
  useEffect(() => {
    fetchRounds();
  }, [fetchRounds]);

  // Final fetch when generation completes (isGenerating: true → false)
  useEffect(() => {
    if (prevIsGenerating.current && !isGenerating) {
      fetchRounds();
    }
    prevIsGenerating.current = isGenerating;
  }, [isGenerating, fetchRounds]);

  // Poll the stories endpoint while generating
  useEffect(() => {
    if (!isGenerating) {
      if (pollingRef.current) clearInterval(pollingRef.current);
      return;
    }
    pollingRef.current = setInterval(fetchRounds, POLL_INTERVAL_MS);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [isGenerating, fetchRounds]);

  async function handleGenerate() {
    setSubmitting(true);
    setError(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/generate`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt: prompt.trim() || null }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error ?? "Something went wrong");
        return;
      }
      setPrompt("");
      onGenerated(); // tell StatusPoller to resume polling
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setSubmitting(false);
    }
  }

  const showPlaceholders = isGenerating && rounds.length === 0;

  return (
    <section className="flex flex-col gap-6">
      <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500">
        Story Options
      </h2>

      {/* Generation rounds — oldest first, latest expanded */}
      {rounds.length > 0 && (
        <div className="flex flex-col gap-6 divide-y divide-zinc-100 dark:divide-zinc-800">
          {rounds.map((round, idx) => (
            <div key={round.id} className={idx > 0 ? "pt-6" : undefined}>
              <StoryRound
                round={round}
                clips={clips}
                defaultExpanded={idx === rounds.length - 1}
              />
            </div>
          ))}
        </div>
      )}

      {/* Loading placeholders while first generation is in-flight */}
      {showPlaceholders && (
        <div className="flex flex-col gap-4">
          {[1, 2, 3].map((i) => (
            <div
              key={i}
              className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-4 flex flex-col gap-3 animate-pulse"
            >
              <div className="flex items-center justify-between">
                <span className="text-xs text-zinc-400 dark:text-zinc-500">
                  Option {i}
                </span>
                <span className="text-xs text-blue-500">Generating·</span>
              </div>
              <div className="h-4 w-2/3 rounded-full bg-zinc-100 dark:bg-zinc-800" />
              <div className="h-10 rounded-lg bg-zinc-100 dark:bg-zinc-800" />
            </div>
          ))}
        </div>
      )}

      {/* Generate more options — shown once at least one round exists */}
      {rounds.length > 0 && (
        <div className="rounded-xl border border-dashed border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 px-4 py-4 flex flex-col gap-3">
          <p className="text-xs font-medium text-zinc-400 dark:text-zinc-500">
            Generate another set of options
          </p>
          <textarea
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            placeholder='e.g. "shorter", "focus on the chorus", "start with the laugh"'
            rows={2}
            className="w-full resize-none rounded-lg border border-zinc-200 dark:border-zinc-700 bg-zinc-50 dark:bg-zinc-800 px-3 py-2 text-sm text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 dark:placeholder:text-zinc-500 focus:outline-none focus:ring-2 focus:ring-blue-500"
          />
          {error && <p className="text-xs text-red-500">{error}</p>}
          <button
            onClick={handleGenerate}
            disabled={submitting || isGenerating}
            className="w-full rounded-xl bg-zinc-800 hover:bg-zinc-700 dark:bg-zinc-700 dark:hover:bg-zinc-600 disabled:opacity-40 disabled:cursor-not-allowed px-4 py-2.5 text-sm font-semibold text-white transition-colors"
          >
            {submitting || isGenerating ? "Generating…" : "Generate more options"}
          </button>
        </div>
      )}
    </section>
  );
}
