"use client";

import { useEffect, useRef, useState } from "react";
import { UploadArea } from "./UploadArea";
import { TranscriptViewer } from "./TranscriptViewer";
import { RangePicker, type ClipMeta } from "./RangePicker";

// ── types ──────────────────────────────────────────────────────────────────

export type ClipStatus =
  | "uploading"
  | "uploading_complete"
  | "transcribing"
  | "transcribed_raw"
  | "aligned"
  | "error";

export type ProjectStatus =
  | "uploading"
  | "transcribing"
  | "transcribed"
  | "rendering"
  | "done"
  | "error";

export type StoryStatus = "generating" | "rendering" | "done" | "error";

interface Clip {
  id: string;
  filename: string;
  status: ClipStatus;
  error_message?: string | null;
  duration_secs?: number | null;
}

interface Project {
  id: string;
  name: string;
  status: ProjectStatus;
}

export interface Story {
  id: string;
  status: StoryStatus;
  created_at: string;
}

// ── constants ──────────────────────────────────────────────────────────────

const TERMINAL_STATUSES: ProjectStatus[] = ["transcribed", "done", "error"];
const POLL_INTERVAL_MS = 3000;

// ── helpers ───────────────────────────────────────────────────────────────

function clipLabel(status: ClipStatus): string {
  switch (status) {
    case "uploading":      return "Uploading";
    case "uploading_complete": return "Queued";
    case "transcribing":  return "Transcribing";
    case "transcribed_raw": return "Transcribed";
    case "aligned":       return "Aligned";
    case "error":         return "Error";
    default:              return status;
  }
}

function clipColor(status: ClipStatus): string {
  switch (status) {
    case "aligned":       return "text-green-600 dark:text-green-400";
    case "error":         return "text-red-500";
    case "transcribing":
    case "uploading":     return "text-blue-500";
    case "transcribed_raw": return "text-amber-500 dark:text-amber-400";
    default:              return "text-zinc-500 dark:text-zinc-400";
  }
}

function clipIcon(status: ClipStatus): string {
  switch (status) {
    case "aligned":       return "✓";
    case "error":         return "✗";
    case "transcribing":
    case "uploading":     return "…";
    case "transcribed_raw": return "⏳";
    default:              return "·";
  }
}

function storyBadge(status: StoryStatus): { label: string; cls: string } {
  switch (status) {
    case "done":      return { label: "Ready",     cls: "text-green-600 dark:text-green-400" };
    case "rendering": return { label: "Rendering", cls: "text-blue-500" };
    case "error":     return { label: "Error",     cls: "text-red-500" };
    default:          return { label: status,      cls: "text-zinc-400" };
  }
}

function fmtDate(iso: string): string {
  return new Date(iso).toLocaleString(undefined, {
    month: "short", day: "numeric",
    hour: "numeric", minute: "2-digit",
  });
}

function bannerText(
  status: ProjectStatus,
  clips: Clip[],
): { text: string; color: string } | null {
  switch (status) {
    case "uploading":
      return { text: "Waiting for uploads to finish…", color: "text-zinc-500" };
    case "transcribing": {
      const done = clips.filter(
        (c) => c.status === "transcribed_raw" || c.status === "aligned",
      ).length;
      const total = clips.length;
      const aligning = clips.every(
        (c) => c.status === "transcribed_raw" || c.status === "aligned",
      );
      if (aligning) {
        return {
          text: "All clips transcribed — running alignment…",
          color: "text-amber-500 dark:text-amber-400",
        };
      }
      return {
        text: total > 0
          ? `Transcribing clips… (${done} of ${total} done)`
          : "Transcribing clips…",
        color: "text-blue-500",
      };
    }
    case "transcribed":
      return {
        text: "Transcription complete — ready to pick ranges",
        color: "text-green-600 dark:text-green-400",
      };
    case "error":
      return { text: "An error occurred", color: "text-red-500" };
    default:
      return null;
  }
}

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  projectId: string;
  initialProject: Project;
  initialClips: Clip[];
  initialStories: Story[];
}

export function StatusPoller({
  projectId,
  initialProject,
  initialClips,
  initialStories,
}: Props) {
  const [project, setProject] = useState<Project>(initialProject);
  const [clips, setClips] = useState<Clip[]>(initialClips);
  const [stories, setStories] = useState<Story[]>(initialStories);
  const alignTriggeredRef = useRef(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Trigger alignment once when all clips reach transcribed_raw
  useEffect(() => {
    if (alignTriggeredRef.current) return;
    if (project.status !== "transcribing") return;
    if (clips.length === 0) return;

    const allRaw = clips.every((c) => c.status === "transcribed_raw");
    if (!allRaw) return;

    alignTriggeredRef.current = true;
    console.log("[poll] all clips transcribed_raw → triggering align");
    fetch(`/api/projects/${projectId}/align`, { method: "POST" }).catch(
      (err) => console.error("[poll] align trigger failed:", err),
    );
  }, [clips, project.status, projectId]);

  // Poll while project is in a non-terminal state
  useEffect(() => {
    const isTerminal = TERMINAL_STATUSES.includes(project.status);
    if (isTerminal) {
      if (pollingRef.current) clearInterval(pollingRef.current);
      return;
    }

    async function poll() {
      try {
        const res = await fetch(`/api/projects/${projectId}`);
        if (!res.ok) return;
        const data = await res.json();
        setProject({ id: data.id, name: data.name, status: data.status });
        setClips(data.clips ?? []);
        setStories(data.stories ?? []);
      } catch {
        // Network blip — keep polling
      }
    }

    pollingRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [project.status, projectId]);

  const banner = bannerText(project.status, clips);
  const isTerminal = TERMINAL_STATUSES.includes(project.status);
  const hasAlignedClips = clips.some((c) => c.status === "aligned");

  return (
    <div className="flex flex-col gap-5">
      {/* Status banner */}
      {banner && (
        <p className={`text-sm ${banner.color}`}>
          {banner.text}
          {!isTerminal && (
            <span className="ml-1 inline-block animate-pulse">·</span>
          )}
        </p>
      )}

      {/* Clip list */}
      {clips.length > 0 && (
        <section>
          <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 mb-2">
            Clips
          </h2>
          <ul className="flex flex-col gap-2">
            {clips.map((clip) => (
              <li
                key={clip.id}
                className="flex flex-col gap-1 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
              >
                <div className="flex items-center justify-between gap-2">
                  <span className="text-sm truncate text-zinc-800 dark:text-zinc-200">
                    {clip.filename}
                  </span>
                  <span className={`shrink-0 text-xs font-mono ${clipColor(clip.status)}`}>
                    {clipIcon(clip.status)} {clipLabel(clip.status)}
                  </span>
                </div>
                {clip.status === "error" && clip.error_message && (
                  <p className="text-xs text-red-500 truncate">{clip.error_message}</p>
                )}
              </li>
            ))}
          </ul>
        </section>
      )}

      {/* Transcript viewer — shown once alignment is done */}
      {hasAlignedClips && (
        <TranscriptViewer projectId={project.id} />
      )}

      {/* Existing cuts */}
      {stories.length > 0 && (
        <section>
          <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 mb-2">
            Your Cuts
          </h2>
          <ul className="flex flex-col gap-2">
            {stories
              .slice()
              .sort((a, b) => new Date(b.created_at).getTime() - new Date(a.created_at).getTime())
              .map((story) => {
                const badge = storyBadge(story.status);
                return (
                  <li key={story.id}>
                    <a
                      href={`/stories/${story.id}`}
                      className="flex items-center justify-between gap-3 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800 transition-colors"
                    >
                      <span className="text-sm text-zinc-700 dark:text-zinc-300">
                        {fmtDate(story.created_at)}
                      </span>
                      <span className={`shrink-0 text-xs font-medium ${badge.cls}`}>
                        {badge.label}
                      </span>
                    </a>
                  </li>
                );
              })}
          </ul>
        </section>
      )}

      {/* Range picker — always shown while aligned clips exist */}
      {hasAlignedClips && (
        <RangePicker
          projectId={project.id}
          clips={clips
            .filter((c) => c.status === "aligned")
            .map((c): ClipMeta => ({
              id: c.id,
              filename: c.filename,
              duration_secs: c.duration_secs ?? null,
            }))}
        />
      )}

      {/* Upload area — only visible while project is still uploading */}
      {project.status === "uploading" && (
        <section>
          {clips.length > 0 && (
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 mb-2">
              Add More
            </h2>
          )}
          <UploadArea projectId={projectId} />
        </section>
      )}
    </div>
  );
}
