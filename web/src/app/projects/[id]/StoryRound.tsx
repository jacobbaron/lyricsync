"use client";

import { useState } from "react";
import { StoryCard, type StoryData } from "./StoryCard";
import type { ClipMeta } from "./RangePicker";

// ── types ─────────────────────────────────────────────────────────────────

export interface RoundData {
  id: string;
  round: number;
  prompt: string | null;
  created_at: string;
  stories: StoryData[];
}

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  round: RoundData;
  clips: ClipMeta[];
  defaultExpanded?: boolean;
}

export function StoryRound({ round, clips, defaultExpanded = false }: Props) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  return (
    <div className="flex flex-col gap-3">
      {/* Round header — collapsible toggle */}
      <button
        onClick={() => setExpanded((v) => !v)}
        className="flex items-center justify-between gap-3 w-full text-left group"
      >
        <div className="flex items-center gap-2 min-w-0">
          <span className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 shrink-0">
            Round {round.round}
          </span>
          {round.prompt && (
            <span className="text-xs text-zinc-500 dark:text-zinc-400 italic truncate">
              &ldquo;{round.prompt}&rdquo;
            </span>
          )}
        </div>
        <span className="text-zinc-400 dark:text-zinc-600 text-xs shrink-0 group-hover:text-zinc-600 dark:group-hover:text-zinc-400 transition-colors">
          {expanded ? "▲" : "▼"}
        </span>
      </button>

      {/* Story cards */}
      {expanded && (
        <div className="flex flex-col gap-4">
          {round.stories.map((story, idx) => (
            <StoryCard
              key={story.id}
              story={story}
              clips={clips}
              index={idx + 1}
            />
          ))}
        </div>
      )}
    </div>
  );
}
