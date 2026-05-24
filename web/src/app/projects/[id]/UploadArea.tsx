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

function ClipRow({ entry }: { entry: UploadEntry }) {
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
      {isError && entry.error && (
        <p className="text-xs text-red-500">{entry.error}</p>
      )}
      {isInterrupted && (
        <p className="text-xs text-amber-600 dark:text-amber-400">
          Upload was interrupted. Refresh the page to retry.
        </p>
      )}
    </div>
  );
}

function randomId() {
  return Math.random().toString(36).slice(2);
}

export function UploadArea({ projectId }: { projectId: string }) {
  const [uploads, setUploads] = useState<UploadEntry[]>([]);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const router = useRouter();

  // Track in-progress XHRs so we can mark them interrupted on visibility change
  const xhrMap = useRef<Map<string, XMLHttpRequest>>(new Map());

  // On visibility change, mark any still-uploading entries as interrupted if
  // XHR already ended (browser may have paused or aborted them in background).
  useEffect(() => {
    function handleVisibility() {
      if (document.visibilityState === "visible") {
        setUploads((prev) =>
          prev.map((u) => {
            if (
              (u.status === "uploading" || u.status === "preparing") &&
              !xhrMap.current.has(u.id)
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

  const updateEntry = useCallback(
    (id: string, updates: Partial<UploadEntry>) => {
      setUploads((prev) =>
        prev.map((u) => (u.id === id ? { ...u, ...updates } : u)),
      );
    },
    [],
  );

  const uploadFile = useCallback(
    async (entry: UploadEntry) => {
      const { id, file } = entry;

      try {
        // Step 1: Request presigned URL + create clip row
        const prepRes = await fetch(`/api/projects/${projectId}/clips`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            filename: file.name,
            contentType: file.type || "video/mp4",
          }),
        });
        if (!prepRes.ok) {
          const body = await prepRes.json().catch(() => ({}));
          throw new Error(body.error ?? `Server error ${prepRes.status}`);
        }
        const { clipId, uploadUrl } = await prepRes.json();
        updateEntry(id, { clipId, status: "uploading", progress: 0 });

        // Step 2: PUT directly to R2 using XHR for progress events
        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhrMap.current.set(id, xhr);

          xhr.upload.addEventListener("progress", (e) => {
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

        // Step 4: Trigger transcription (no-op until P1-06 is deployed)
        fetch(`/api/clips/${clipId}/transcribe`, { method: "POST" }).catch(
          () => {
            // Silently ignore — endpoint doesn't exist yet (P1-06)
          },
        );

        updateEntry(id, { status: "done", progress: 100 });
      } catch (err) {
        xhrMap.current.delete(id);
        updateEntry(id, {
          status: "error",
          error: err instanceof Error ? err.message : "Upload failed",
        });
      }
    },
    [projectId, updateEntry],
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

      // Start all uploads in parallel; once all settle, refresh the server
      // component and drop completed entries (they graduate to the Clips list).
      Promise.all(newEntries.map(uploadFile)).then(() => {
        router.refresh();
        setUploads((prev) => prev.filter((u) => u.status !== "done"));
      });
    },
    [uploadFile, router],
  );

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
              <ClipRow entry={entry} />
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
