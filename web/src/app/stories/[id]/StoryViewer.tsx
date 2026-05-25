"use client";

import { useEffect, useRef, useState } from "react";

// ── types ─────────────────────────────────────────────────────────────────

type StoryStatus = "generating" | "rendering" | "done" | "error";

interface Story {
  id: string;
  project_id: string;
  status: StoryStatus;
  error_message?: string | null;
  render_r2_key?: string | null;
}

// ── constants ─────────────────────────────────────────────────────────────

const TERMINAL: StoryStatus[] = ["done", "error"];
const POLL_MS = 3000;

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  storyId: string;
  initialStory: Story;
}

export function StoryViewer({ storyId, initialStory }: Props) {
  const [story, setStory] = useState<Story>(initialStory);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Poll while non-terminal
  useEffect(() => {
    if (TERMINAL.includes(story.status)) {
      if (pollingRef.current) clearInterval(pollingRef.current);
      return;
    }

    async function poll() {
      try {
        const res = await fetch(`/api/stories/${storyId}`);
        if (!res.ok) return;
        const data = await res.json();
        setStory(data);
      } catch {
        // Network blip — keep polling
      }
    }

    pollingRef.current = setInterval(poll, POLL_MS);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [story.status, storyId]);

  // ── render ──────────────────────────────────────────────────────────────

  if (story.status === "error") {
    return (
      <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 px-5 py-4 flex flex-col gap-2">
        <p className="text-sm font-medium text-red-600 dark:text-red-400">
          Render failed
        </p>
        {story.error_message && (
          <p className="text-xs text-red-500 font-mono">{story.error_message}</p>
        )}
      </div>
    );
  }

  if (story.status === "done" && story.render_r2_key) {
    // P1-12 will add inline video playback and a download button here.
    return (
      <div className="rounded-xl border border-green-200 dark:border-green-900 bg-green-50 dark:bg-green-950 px-5 py-4 flex flex-col gap-3">
        <p className="text-sm font-medium text-green-700 dark:text-green-400">
          Cut ready
        </p>
        <p className="text-xs text-zinc-500 dark:text-zinc-400">
          Playback and download will be available in the next update.
        </p>
      </div>
    );
  }

  // Rendering / generating
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-5 py-6 flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <span className="text-sm text-blue-500">
          Rendering cut
          <span className="ml-1 inline-block animate-pulse">·</span>
        </span>
      </div>
      <div className="flex flex-col gap-2 animate-pulse">
        {[80, 60, 75].map((w, i) => (
          <div
            key={i}
            className="h-3 rounded-full bg-zinc-100 dark:bg-zinc-800"
            style={{ width: `${w}%` }}
          />
        ))}
      </div>
      <p className="text-xs text-zinc-400 dark:text-zinc-500">
        This usually takes a minute or two. You can leave this page and come back.
      </p>
    </div>
  );
}
