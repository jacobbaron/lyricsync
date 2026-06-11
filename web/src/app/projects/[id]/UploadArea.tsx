"use client";

import { useRef, useState, useCallback, useEffect } from "react";
import { useRouter } from "next/navigation";

interface UploadEntry {
  id: string;
  file: File;
  clipId?: string;
  progress: number; // 0–100
  status: "preparing" | "uploading" | "done" | "error" | "interrupted";
  error?: string;
}

// No bytes sent and no server response for this long → the transfer is dead
// (iOS suspends background XHRs without firing any terminal event).
const STALL_TIMEOUT_MS = 90_000;
const STALL_CHECK_INTERVAL_MS = 10_000;

function ProgressBar({ value }: { value: number }) {
  return (
    <div className="h-1 w-full rounded-full bg-zinc-200 dark:bg-zinc-700 overflow-hidden">
      <div
        className="h-full rounded-full bg-zinc-900 dark:bg-white transition-all duration-150"
        style={{ width: `${value}%` }}
      />
    </div>
  );
}

function ClipRow({
  entry,
  onRetry,
  onDismiss,
}: {
  entry: UploadEntry;
  onRetry: (id: string) => void;
  onDismiss: (id: string) => void;
}) {
  const isActive = entry.status === "preparing" || entry.status === "uploading";
  const isDone = entry.status === "done";
  const isError = entry.status === "error";
  const isInterrupted = entry.status === "interrupted";

  return (
    <div className="flex flex-col gap-1.5 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900">
      <div className="flex items-center justify-between gap-2">
        <span className="text-sm truncate text-zinc-800 dark:text-zinc-200">
          {entry.file.name}
        </span>
        <span className="shrink-0 text-xs">
          {entry.status === "preparing" && (
            <span className="text-zinc-400">Preparing…</span>
          )}
          {entry.status === "uploading" && (
            <span className="text-zinc-500">{entry.progress}%</span>
          )}
          {isDone && (
            <span className="text-green-600 dark:text-green-400">✓ Uploaded</span>
          )}
          {isError && (
            <span className="text-red-500">Failed</span>
          )}
          {isInterrupted && (
            <span className="text-amber-500">Interrupted</span>
          )}
        </span>
      </div>
      {isActive && <ProgressBar value={entry.progress} />}
      {(isError || isInterrupted) && (
        <>
          <p className={`text-xs ${isError ? "text-red-500" : "text-amber-600 dark:text-amber-400"}`}>
            {entry.error ?? "Upload was interrupted."}
          </p>
          <div className="flex items-center gap-3">
            <button
              onClick={() => onRetry(entry.id)}
              className="text-xs font-semibold text-blue-600 dark:text-blue-400"
            >
              Retry
            </button>
            <button
              onClick={() => onDismiss(entry.id)}
              className="text-xs text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300"
            >
              Dismiss
            </button>
          </div>
        </>
      )}
    </div>
  );
}

function randomId() {
  return Math.random().toString(36).slice(2);
}

export function UploadArea({
  projectId,
  onUploaded,
}: {
  projectId: string;
  onUploaded?: () => void;
}) {
  const [uploads, setUploads] = useState<UploadEntry[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();

  // Track in-flight transfers so the stall watchdog and visibility handler can
  // abort/inspect them: the XHR for the R2 PUT, the AbortController for the
  // prepare fetch, and a last-activity timestamp updated on every progress event.
  const xhrMap = useRef<Map<string, XMLHttpRequest>>(new Map());
  const prepMap = useRef<Map<string, AbortController>>(new Map());
  const lastActivity = useRef<Map<string, number>>(new Map());
  // Entries the watchdog aborted — lets the catch path label them "stalled"
  // instead of the generic abort error.
  const stalledIds = useRef<Set<string>>(new Set());

  // On visibility change, mark any still-uploading entries as interrupted if
  // XHR already ended (browser may have paused or aborted them in background).
  useEffect(() => {
    function handleVisibility() {
      if (document.visibilityState === "visible") {
        setUploads((prev) =>
          prev.map((u) => {
            if (
              (u.status === "uploading" || u.status === "preparing") &&
              !xhrMap.current.has(u.id) &&
              !prepMap.current.has(u.id)
            ) {
              return { ...u, status: "interrupted" };
            }
            return u;
          }),
        );
      }
    }
    document.addEventListener("visibilitychange", handleVisibility);
    return () =>
      document.removeEventListener("visibilitychange", handleVisibility);
  }, []);

  // Stall watchdog: abort transfers that have made no progress for too long so
  // they surface as retryable instead of sitting at "Uploading…" forever (and
  // keeping the Add Videos button disabled).
  useEffect(() => {
    const timer = setInterval(() => {
      const now = Date.now();
      for (const [id, last] of lastActivity.current) {
        if (now - last < STALL_TIMEOUT_MS) continue;
        stalledIds.current.add(id);
        xhrMap.current.get(id)?.abort();
        prepMap.current.get(id)?.abort();
      }
    }, STALL_CHECK_INTERVAL_MS);
    return () => clearInterval(timer);
  }, []);

  const updateEntry = useCallback(
    (id: string, updates: Partial<UploadEntry>) => {
      setUploads((prev) =>
        prev.map((u) => (u.id === id ? { ...u, ...updates } : u)),
      );
    },
    [],
  );

  const uploadFile = useCallback(
    async (entry: UploadEntry): Promise<boolean> => {
      const { id, file } = entry;
      lastActivity.current.set(id, Date.now());

      try {
        // Step 1: Request presigned URL + create clip row
        const prepAc = new AbortController();
        prepMap.current.set(id, prepAc);
        let prepRes: Response;
        try {
          prepRes = await fetch(`/api/projects/${projectId}/clips`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
              filename: file.name,
              contentType: file.type || "video/mp4",
            }),
            signal: prepAc.signal,
          });
        } finally {
          prepMap.current.delete(id);
        }
        if (!prepRes.ok) {
          const body = await prepRes.json().catch(() => ({}));
          throw new Error(body.error ?? `Server error ${prepRes.status}`);
        }
        const { clipId, uploadUrl } = await prepRes
          .json()
          .catch(() => ({} as { clipId?: string; uploadUrl?: string }));
        if (!clipId || !uploadUrl) {
          throw new Error("Server returned an unexpected response");
        }
        updateEntry(id, { clipId, status: "uploading", progress: 0 });
        lastActivity.current.set(id, Date.now());

        // Step 2: PUT directly to R2 using XHR for progress events
        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhrMap.current.set(id, xhr);

          xhr.upload.addEventListener("progress", (e) => {
            lastActivity.current.set(id, Date.now());
            if (e.lengthComputable) {
              updateEntry(id, {
                progress: Math.round((e.loaded / e.total) * 100),
              });
            }
          });
          xhr.addEventListener("load", () => {
            xhrMap.current.delete(id);
            if (xhr.status >= 200 && xhr.status < 300) {
              resolve();
            } else {
              reject(new Error(`Upload failed (HTTP ${xhr.status})`));
            }
          });
          xhr.addEventListener("error", () => {
            xhrMap.current.delete(id);
            reject(new Error("Network error — check your connection"));
          });
          xhr.addEventListener("abort", () => {
            xhrMap.current.delete(id);
            reject(new Error("Upload cancelled"));
          });

          xhr.open("PUT", uploadUrl);
          xhr.setRequestHeader("Content-Type", file.type || "video/mp4");
          xhr.send(file);
        });

        // Step 3: Mark clip as uploading_complete in DB
        const patchRes = await fetch(`/api/clips/${clipId}`, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ status: "uploading_complete" }),
        });
        if (!patchRes.ok) {
          throw new Error("Failed to confirm upload — server error");
        }

        // Step 4: Trigger transcription. Failure is recoverable — the clip
        // list shows "Queued" with a retry button — but log it loudly.
        fetch(`/api/clips/${clipId}/transcribe`, { method: "POST" }).catch(
          (err) => console.error("[upload] transcribe trigger failed:", err),
        );

        lastActivity.current.delete(id);
        updateEntry(id, { status: "done", progress: 100 });
        return true;
      } catch (err) {
        xhrMap.current.delete(id);
        prepMap.current.delete(id);
        lastActivity.current.delete(id);
        if (stalledIds.current.delete(id)) {
          updateEntry(id, {
            status: "interrupted",
            error: "Upload stalled — no progress for 90 seconds.",
          });
        } else {
          updateEntry(id, {
            status: "error",
            error: err instanceof Error ? err.message : "Upload failed",
          });
        }
        return false;
      }
    },
    [projectId, updateEntry],
  );

  // Shared completion: refresh the server component, drop finished entries
  // (they graduate to the Clips list), resume parent polling if anything landed.
  const finalize = useCallback(
    (results: boolean[]) => {
      router.refresh();
      setUploads((prev) => prev.filter((u) => u.status !== "done"));
      if (results.some((ok) => ok)) onUploaded?.();
    },
    [router, onUploaded],
  );

  const handleFiles = useCallback(
    (files: FileList) => {
      const newEntries: UploadEntry[] = Array.from(files).map((file) => ({
        id: randomId(),
        file,
        progress: 0,
        status: "preparing",
      }));

      setUploads((prev) => [...prev, ...newEntries]);
      Promise.all(newEntries.map(uploadFile)).then(finalize);
    },
    [uploadFile, finalize],
  );

  const handleRetry = useCallback(
    (id: string) => {
      setUploads((prev) => {
        const entry = prev.find((u) => u.id === id);
        if (!entry) return prev;
        // Drop the clip row from the failed attempt (best-effort) — the retry
        // creates a fresh row with a fresh presigned URL.
        if (entry.clipId) {
          fetch(`/api/clips/${entry.clipId}`, { method: "DELETE" }).catch(
            () => {},
          );
        }
        const fresh: UploadEntry = {
          id: entry.id,
          file: entry.file,
          progress: 0,
          status: "preparing",
        };
        uploadFile(fresh).then((ok) => finalize([ok]));
        return prev.map((u) => (u.id === id ? fresh : u));
      });
    },
    [uploadFile, finalize],
  );

  const handleDismiss = useCallback((id: string) => {
    setUploads((prev) => {
      const entry = prev.find((u) => u.id === id);
      if (entry?.clipId) {
        fetch(`/api/clips/${entry.clipId}`, { method: "DELETE" }).catch(
          () => {},
        );
      }
      return prev.filter((u) => u.id !== id);
    });
  }, []);

  const anyActive = uploads.some(
    (u) => u.status === "preparing" || u.status === "uploading",
  );

  return (
    <div className="flex flex-col gap-3">
      <input
        ref={fileInputRef}
        type="file"
        accept="video/*"
        multiple
        className="hidden"
        onChange={(e) => {
          if (e.target.files && e.target.files.length > 0) {
            handleFiles(e.target.files);
            // Reset so the same files can be re-selected after an error
            e.target.value = "";
          }
        }}
      />

      <button
        onClick={() => fileInputRef.current?.click()}
        disabled={anyActive}
        className="flex items-center justify-center gap-2 rounded-xl border-2 border-dashed border-zinc-300 py-4 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400 disabled:opacity-50"
      >
        <span className="text-lg">＋</span>
        {anyActive ? "Uploading…" : "Add Videos"}
      </button>

      {uploads.length > 0 && (
        <ul className="flex flex-col gap-2">
          {uploads.map((entry) => (
            <li key={entry.id}>
              <ClipRow
                entry={entry}
                onRetry={handleRetry}
                onDismiss={handleDismiss}
              />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
