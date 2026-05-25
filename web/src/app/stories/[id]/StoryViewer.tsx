"use client";

import { useEffect, useRef, useState, useCallback } from "react";

// ── types ─────────────────────────────────────────────────────────────────

type StoryStatus = "generating" | "rendering" | "done" | "error";

interface Story {
  id: string;
  project_id: string;
  status: StoryStatus;
  error_message?: string | null;
  render_r2_key?: string | null;
}

interface SignedUrls {
  playback_url: string;
  download_url: string;
}

// ── constants ─────────────────────────────────────────────────────────────

const TERMINAL: StoryStatus[] = ["done", "error"];
const POLL_MS = 3000;

// ── icons ─────────────────────────────────────────────────────────────────

function DownloadIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M10.75 2.75a.75.75 0 0 0-1.5 0v8.614L6.295 8.235a.75.75 0 1 0-1.09 1.03l4.25 4.5a.75.75 0 0 0 1.09 0l4.25-4.5a.75.75 0 0 0-1.09-1.03l-2.955 3.129V2.75Z" />
      <path d="M3.5 12.75a.75.75 0 0 0-1.5 0v2.5A2.75 2.75 0 0 0 4.75 18h10.5A2.75 2.75 0 0 0 18 15.25v-2.5a.75.75 0 0 0-1.5 0v2.5c0 .69-.56 1.25-1.25 1.25H4.75c-.69 0-1.25-.56-1.25-1.25v-2.5Z" />
    </svg>
  );
}

function ShareIcon() {
  return (
    <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20" fill="currentColor" className="w-4 h-4 shrink-0">
      <path d="M13 4.5a2.5 2.5 0 1 1 .702 1.737L6.97 9.604a2.518 2.518 0 0 1 0 .792l6.733 3.367a2.5 2.5 0 1 1-.671 1.341l-6.733-3.367a2.5 2.5 0 1 1 0-3.475l6.733-3.366A2.52 2.52 0 0 1 13 4.5Z" />
    </svg>
  );
}

// ── component ─────────────────────────────────────────────────────────────

interface Props {
  storyId: string;
  initialStory: Story;
}

export function StoryViewer({ storyId, initialStory }: Props) {
  const [story, setStory] = useState<Story>(initialStory);
  const [urls, setUrls] = useState<SignedUrls | null>(null);
  const [urlError, setUrlError] = useState<string | null>(null);
  const [retrying, setRetrying] = useState(false);
  const [sharing, setSharing] = useState(false);
  const [shareError, setShareError] = useState<string | null>(null);
  // Pre-fetched blob for Web Share API — must be ready before the tap so
  // navigator.share() can be called synchronously within the user gesture.
  const [videoBlob, setVideoBlob] = useState<Blob | null>(null);
  const [blobLoading, setBlobLoading] = useState(false);
  const pollingRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // True once we know the browser supports file sharing (checked client-side)
  const canShare = typeof navigator !== "undefined" && "share" in navigator && "canShare" in navigator;

  // Poll for status changes while non-terminal
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

  // Fetch signed URLs once the story is done
  useEffect(() => {
    if (story.status !== "done") return;
    fetch(`/api/stories/${storyId}/signed-url`)
      .then((r) => r.json())
      .then((data) => {
        if (data.error) throw new Error(data.error);
        setUrls(data);
      })
      .catch((err: Error) => setUrlError(err.message));
  }, [story.status, storyId]);

  // Pre-fetch the video blob for Web Share API as soon as the story is done.
  // navigator.share() requires transient user activation — any async work
  // inside the click handler (e.g. a fetch) expires the gesture before share()
  // is reached, causing "not allowed" errors. Keeping the blob ready in state
  // means the tap handler calls share() synchronously with no I/O in between.
  useEffect(() => {
    if (story.status !== "done" || !canShare) return;
    setBlobLoading(true);
    fetch(`/api/stories/${storyId}/video`)
      .then((r) => (r.ok ? r.blob() : Promise.reject(new Error("fetch failed"))))
      .then((blob) => setVideoBlob(blob))
      .catch(() => { /* silently fail — share button stays hidden */ })
      .finally(() => setBlobLoading(false));
  }, [story.status, storyId, canShare]);

  // Retry — resets story to rendering and restarts the poll
  async function handleRetry() {
    setRetrying(true);
    try {
      await fetch(`/api/stories/${storyId}/render`, { method: "POST" });
      setStory((prev) => ({
        ...prev,
        status: "rendering",
        error_message: null,
      }));
    } finally {
      setRetrying(false);
    }
  }

  // Share to Photos via Web Share API.
  // iOS Safari: pass ONLY `files` (no title/text/url) — otherwise file
  // sharing silently fails. The Share Sheet will include "Save to Photos".
  //
  // The blob is pre-fetched eagerly (see effect above) so this handler can
  // call navigator.share() synchronously within the user gesture — any async
  // work here would expire transient activation before share() is reached.
  const handleSaveToPhotos = useCallback(async () => {
    if (!videoBlob) return;
    setSharing(true);
    setShareError(null);
    try {
      const file = new File([videoBlob], "output.mp4", { type: "video/mp4" });

      if (!navigator.canShare({ files: [file] })) {
        throw new Error("Your browser doesn't support sharing video files");
      }

      // iOS requirement: files only — no other properties in the share object
      await navigator.share({ files: [file] });
    } catch (err) {
      // AbortError = user dismissed the sheet — not an error
      if (err instanceof Error && err.name !== "AbortError") {
        setShareError(err.message);
      }
    } finally {
      setSharing(false);
    }
  }, [videoBlob]);

  // ── render ───────────────────────────────────────────────────────────────

  if (story.status === "done") {
    return (
      <div className="flex flex-col gap-4">
        {/* Video player */}
        <div className="rounded-xl overflow-hidden bg-black">
          {urls ? (
            <video
              src={urls.playback_url}
              controls
              playsInline
              className="w-full max-h-[70vh]"
            />
          ) : urlError ? (
            <div className="flex items-center justify-center h-48 text-sm text-red-400 px-4 text-center">
              Could not load video: {urlError}
            </div>
          ) : (
            <div className="flex items-center justify-center h-48 animate-pulse bg-zinc-900">
              <span className="text-zinc-600 text-sm">Loading…</span>
            </div>
          )}
        </div>

        {urls && (
          <div className="flex flex-col gap-3">
            {/* Save to Photos — Web Share API, iOS only */}
            {canShare && (
              <button
                onClick={handleSaveToPhotos}
                disabled={sharing || blobLoading || !videoBlob}
                className="flex items-center justify-center gap-2 w-full rounded-xl bg-blue-600 hover:bg-blue-700 disabled:opacity-50 disabled:cursor-not-allowed px-4 py-3 text-sm font-semibold text-white transition-colors"
              >
                <ShareIcon />
                {blobLoading ? "Preparing…" : sharing ? "Opening…" : "Save to Photos"}
              </button>
            )}

            {shareError && (
              <p className="text-xs text-red-500 text-center px-1">{shareError}</p>
            )}

            {/* Save to Files — always available, Content-Disposition:attachment */}
            <a
              href={urls.download_url}
              className="flex items-center justify-center gap-2 w-full rounded-xl border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800 px-4 py-3 text-sm font-semibold text-zinc-800 dark:text-zinc-200 transition-colors"
            >
              <DownloadIcon />
              Save to Files
            </a>
          </div>
        )}
      </div>
    );
  }

  if (story.status === "error") {
    return (
      <div className="flex flex-col gap-4">
        <div className="rounded-xl border border-red-200 dark:border-red-900 bg-red-50 dark:bg-red-950 px-5 py-4 flex flex-col gap-2">
          <p className="text-sm font-medium text-red-600 dark:text-red-400">
            Render failed
          </p>
          {story.error_message && (
            <p className="text-xs text-red-500 font-mono break-words">
              {story.error_message}
            </p>
          )}
        </div>
        <button
          onClick={handleRetry}
          disabled={retrying}
          className="w-full rounded-xl border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800 disabled:opacity-40 disabled:cursor-not-allowed px-4 py-3 text-sm font-semibold text-zinc-800 dark:text-zinc-200 transition-colors"
        >
          {retrying ? "Starting…" : "Retry render"}
        </button>
      </div>
    );
  }

  // Rendering / generating — pulse skeleton
  return (
    <div className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-5 py-6 flex flex-col gap-4">
      <p className="text-sm text-blue-500">
        Rendering cut
        <span className="ml-1 inline-block animate-pulse">·</span>
      </p>
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
        This usually takes a minute or two. You can leave this page and come
        back.
      </p>
    </div>
  );
}
