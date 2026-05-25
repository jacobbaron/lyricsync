"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { UploadArea } from "./UploadArea";
import { TranscriptViewer } from "./TranscriptViewer";
import { StoryGenerator } from "./StoryGenerator";
import { StoryBrowser } from "./StoryBrowser";
import type { ClipMeta } from "./RangePicker";

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
  | "generating_stories"
  | "stories_ready"
  | "rendering"
  | "done"
  | "error";

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

// ── constants ──────────────────────────────────────────────────────────────

// Statuses where the main project-level poller should stop
const TERMINAL_STATUSES: ProjectStatus[] = [
  "stories_ready",
  "done",
  "error",
];

const POLL_INTERVAL_MS = 3000;

// ── helpers ───────────────────────────────────────────────────────────────

function clipLabel(status: ClipStatus): string {
  switch (status) {
    case "uploading": return "Uploading";
    case "uploading_complete": return "Queued";
    case "transcribing": return "Transcribing";
    case "transcribed_raw": return "Transcribed";
    case "aligned": return "Aligned";
    case "error": return "Error";
    default: return status;
  }
}

function clipColor(status: ClipStatus): string {
  switch (status) {
    case "aligned": return "text-green-600 dark:text-green-400";
    case "error": return "text-red-500";
    case "transcribing":
    case "uploading": return "text-blue-500";
    case "transcribed_raw": return "text-amber-500 dark:text-amber-400";
    default: return "text-zinc-500 dark:text-zinc-400";
  }
}

function clipIcon(status: ClipStatus): string {
  switch (status) {
    case "aligned": return "✓";
    case "error": return "✗";
    case "transcribing":
    case "uploading": return "…";
    case "transcribed_raw": return "⏳";
    case "uploading_complete": return "·";
    default: return "·";
  }
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
          text: "All clips transcribed — merging transcripts…",
          color: "text-amber-500 dark:text-amber-400",
        };
      }
      return {
        text:
          total > 0
            ? `Transcribing clips… (${done} of ${total} done)`
            : "Transcribing clips…",
        color: "text-blue-500",
      };
    }
    case "transcribed":
      return {
        text: "Transcription complete — generate story options below",
        color: "text-green-600 dark:text-green-400",
      };
    case "generating_stories":
      return {
        text: "Generating story options…",
        color: "text-blue-500",
      };
    case "stories_ready":
      return {
        text: "Story options ready",
        color: "text-green-600 dark:text-green-400",
      };
    case "rendering":
      return { text: "Rendering cut…", color: "text-blue-500" };
    case "done":
      return { text: "Done", color: "text-green-600 dark:text-green-400" };
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
}

export function StatusPoller({ projectId, initialProject, initialClips }: Props) {
  const [project, setProject] = useState<Project>(initialProject);
  const [clips, setClips] = useState<Clip[]>(initialClips);
  // Start collapsed if every clip is already done (page reload after transcription)
  const [clipsOpen, setClipsOpen] = useState(
    () => initialClips.some((c) => c.status !== "aligned" && c.status !== "error"),
  );
  const alignTriggeredRef = useRef(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  const isTerminal = TERMINAL_STATUSES.includes(project.status);

  // Trigger merge once when all clips reach transcribed_raw.
  // Calls /merge (fast, Vercel-side JSON merge from raw Whisper transcripts).
  // To re-enable WhisperX alignment instead, swap /merge → /align here.
  useEffect(() => {
    if (alignTriggeredRef.current) return;
    if (project.status !== "transcribing") return;
    if (clips.length === 0) return;

    const allRaw = clips.every((c) => c.status === "transcribed_raw");
    if (!allRaw) return;

    alignTriggeredRef.current = true;
    console.log("[poll] all clips transcribed_raw → triggering merge");
    fetch(`/api/projects/${projectId}/merge`, { method: "POST" }).catch(
      (err) => console.error("[poll] merge trigger failed:", err),
    );
  }, [clips, project.status, projectId]);

  // Poll while project is in a non-terminal state
  useEffect(() => {
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
        const updated: Clip[] = data.clips ?? [];
        setClips(updated);
        // Collapse the clip list once all clips finish processing
        if (updated.length > 0 && updated.every((c) => c.status === "aligned" || c.status === "error")) {
          setClipsOpen(false);
        }
      } catch {
        // Network blip — keep polling
      }
    }

    pollingRef.current = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      if (pollingRef.current) clearInterval(pollingRef.current);
    };
  }, [project.status, projectId, isTerminal]);

  // Called by StoryGenerator (first generation) and StoryBrowser (re-generation).
  // Optimistically advances local status so the poller resumes immediately.
  const handleGenerationStarted = useCallback(() => {
    setProject((prev) => ({ ...prev, status: "generating_stories" }));
  }, []);

  const banner = bannerText(project.status, clips);

  // Derived clip list for story components
  const alignedClips: ClipMeta[] = clips
    .filter((c) => c.status === "aligned")
    .map((c) => ({
      id: c.id,
      filename: c.filename,
      duration_secs: c.duration_secs ?? null,
    }));

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

      {/* Clip list — collapsible */}
      {clips.length > 0 && (
        <section>
          <button
            type="button"
            onClick={() => setClipsOpen((o) => !o)}
            className="flex w-full items-center gap-1.5 mb-2 group"
          >
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors">
              Clips
            </h2>
            <span className="text-xs text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors">
              · {clips.length}
            </span>
            <span className="ml-auto text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors text-xs">
              {clipsOpen ? "▾" : "▸"}
            </span>
          </button>
          {clipsOpen && (
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
                    <span
                      className={`shrink-0 text-xs font-mono ${clipColor(clip.status)}`}
                    >
                      {clipIcon(clip.status)} {clipLabel(clip.status)}
                    </span>
                  </div>
                  {clip.status === "error" && clip.error_message && (
                    <p className="text-xs text-red-500 truncate">
                      {clip.error_message}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      {/* Transcript viewer — shown once alignment is done. Stays visible through
          all downstream statuses so users can reference it anytime. */}
      {(
        project.status === "transcribed" ||
        project.status === "generating_stories" ||
        project.status === "stories_ready" ||
        project.status === "rendering" ||
        project.status === "done"
      ) && (
        <TranscriptViewer projectId={project.id} />
      )}

      {/* Story generator — shown when transcription is complete and no stories
          have been generated yet (first generation). */}
      {project.status === "transcribed" && (
        <StoryGenerator
          projectId={project.id}
          onStarted={handleGenerationStarted}
        />
      )}

      {/* Story browser — once stories exist, always show. This includes
          rendering/done so users can navigate back to in-flight or completed
          stories from the project page. */}
      {(
        project.status === "generating_stories" ||
        project.status === "stories_ready" ||
        project.status === "rendering" ||
        project.status === "done"
      ) && (
        <StoryBrowser
          projectId={project.id}
          clips={alignedClips}
          isGenerating={project.status === "generating_stories"}
          onGenerated={handleGenerationStarted}
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
