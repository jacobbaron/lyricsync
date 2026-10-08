"use client";

import { useCallback, useEffect, useRef, useState } from "react";

// Song / audio-master upload + list for a project (issue #160). Upload follows
// the UploadArea pattern — register (POST /api/projects/[id]/songs) → XHR PUT
// to the presigned URL with progress + a stall watchdog → /complete with the
// duration probed here. /complete then syncs the song to every clip; the list
// polls while any sync is running and shows each clip's offset + drift.

type ClipSync = {
  clip_id: string;
  filename: string | null;
  status: "aligning" | "ready" | "error";
  offset: number | null;
  method: string | null;
  confidence: number | null;
  drift_ms: number | null;
  drift_warning: boolean;
  error: string | null;
};

type Song = {
  id: string;
  filename: string | null;
  duration_secs: number | null;
  status: "uploading" | "ready" | "error";
  created_at: string;
  clips: ClipSync[];
};

type Upload = {
  file: File;
  progress: number;
  status: "preparing" | "uploading" | "finishing" | "error";
  error?: string;
};

const ACCEPT = ".wav,.mp3,.m4a,.aif,.aiff,.flac,audio/*";

// Browsers often leave File.type empty for aiff/flac; R2 wants a real type.
const TYPE_BY_EXT: Record<string, string> = {
  wav: "audio/wav",
  mp3: "audio/mpeg",
  m4a: "audio/mp4",
  aif: "audio/aiff",
  aiff: "audio/aiff",
  flac: "audio/flac",
};

// Same thresholds as UploadArea: masters can be big WAVs on flaky mobile links.
const STALL_TIMEOUT_MS = 90_000;
const STALL_CHECK_INTERVAL_MS = 10_000;
const POLL_MS = 4000;

function contentTypeFor(file: File): string {
  if (file.type) return file.type;
  const ext = file.name.split(".").pop()?.toLowerCase() ?? "";
  return TYPE_BY_EXT[ext] ?? "application/octet-stream";
}

// Duration from the browser's decoder; null if it can't read the format (e.g.
// AIFF in Chrome) — the duration is display-only, so that's fine.
function probeDuration(file: File): Promise<number | null> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const audio = new Audio();
    const done = (v: number | null) => {
      URL.revokeObjectURL(url);
      resolve(v);
    };
    audio.preload = "metadata";
    audio.onloadedmetadata = () =>
      done(Number.isFinite(audio.duration) && audio.duration > 0 ? audio.duration : null);
    audio.onerror = () => done(null);
    setTimeout(() => done(null), 15_000);
    audio.src = url;
  });
}

function fmtDuration(s: number | null): string {
  if (s == null) return "—";
  const m = Math.floor(s / 60);
  const sec = Math.round(s % 60);
  return `${m}:${String(sec).padStart(2, "0")}`;
}

function fmtOffset(o: number): string {
  return `${o >= 0 ? "+" : "−"}${Math.abs(o).toFixed(3)}s`;
}

function ClipSyncRow({ c }: { c: ClipSync }) {
  return (
    <li className="flex items-center justify-between gap-2 text-xs">
      <span className="truncate text-zinc-600 dark:text-zinc-400">
        {c.filename ?? c.clip_id.slice(0, 8)}
      </span>
      <span className="shrink-0 text-right">
        {c.status === "aligning" && <span className="text-zinc-400">Syncing…</span>}
        {c.status === "error" && (
          <span className="text-red-500" title={c.error ?? undefined}>
            Sync failed
          </span>
        )}
        {c.status === "ready" && c.offset != null && (
          <span
            className={
              c.drift_warning
                ? "text-amber-600 dark:text-amber-400"
                : "text-zinc-700 dark:text-zinc-300"
            }
            title={
              c.method === "dtw"
                ? "Matched by chroma-DTW (not the same take as the master): lip-sync is approximate."
                : `Same-take match, confidence ${c.confidence ?? "?"}`
            }
          >
            {fmtOffset(c.offset)}
            {c.method === "dtw" && " · approx"}
            {c.drift_warning && ` · drifts ${Math.round(c.drift_ms ?? 0)}ms — lip-sync will wander`}
          </span>
        )}
      </span>
    </li>
  );
}

export function SongsPanel({ projectId }: { projectId: string }) {
  const [songs, setSongs] = useState<Song[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [upload, setUpload] = useState<Upload | null>(null);
  const [busy, setBusy] = useState<string | null>(null); // song id being acted on
  const fileRef = useRef<HTMLInputElement>(null);
  const xhrRef = useRef<XMLHttpRequest | null>(null);
  const lastActivity = useRef<number | null>(null);
  const stalled = useRef(false);

  const load = useCallback(async () => {
    try {
      const res = await fetch(`/api/projects/${projectId}/songs`);
      if (!res.ok) throw new Error(`Server error ${res.status}`);
      const body = (await res.json()) as { songs: Song[] };
      setSongs(body.songs);
      setLoadError(null);
    } catch (err) {
      setLoadError(err instanceof Error ? err.message : "Failed to load songs");
    }
  }, [projectId]);

  useEffect(() => {
    load();
  }, [load]);

  // Poll while anything is still syncing.
  const syncing = (songs ?? []).some((s) =>
    s.clips.some((c) => c.status === "aligning"),
  );
  useEffect(() => {
    if (!syncing) return;
    const t = setInterval(load, POLL_MS);
    return () => clearInterval(t);
  }, [syncing, load]);

  // Stall watchdog for the PUT (see UploadArea).
  useEffect(() => {
    const t = setInterval(() => {
      const last = lastActivity.current;
      if (last != null && Date.now() - last > STALL_TIMEOUT_MS) {
        stalled.current = true;
        xhrRef.current?.abort();
      }
    }, STALL_CHECK_INTERVAL_MS);
    return () => clearInterval(t);
  }, []);

  const uploadFile = useCallback(
    async (file: File) => {
      const contentType = contentTypeFor(file);
      setUpload({ file, progress: 0, status: "preparing" });
      lastActivity.current = Date.now();
      stalled.current = false;
      let songId: string | undefined;
      try {
        const [prep, duration] = await Promise.all([
          fetch(`/api/projects/${projectId}/songs`, {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ filename: file.name, contentType }),
          }),
          probeDuration(file),
        ]);
        if (!prep.ok) {
          const body = await prep.json().catch(() => ({}));
          throw new Error(body.error ?? `Server error ${prep.status}`);
        }
        const { songId: id, uploadUrl } = (await prep.json()) as {
          songId: string;
          uploadUrl: string;
        };
        songId = id;
        setUpload({ file, progress: 0, status: "uploading" });
        lastActivity.current = Date.now();

        await new Promise<void>((resolve, reject) => {
          const xhr = new XMLHttpRequest();
          xhrRef.current = xhr;
          xhr.upload.addEventListener("progress", (e) => {
            lastActivity.current = Date.now();
            if (e.lengthComputable) {
              setUpload({
                file,
                progress: Math.round((e.loaded / e.total) * 100),
                status: "uploading",
              });
            }
          });
          xhr.addEventListener("load", () =>
            xhr.status >= 200 && xhr.status < 300
              ? resolve()
              : reject(new Error(`Upload failed (HTTP ${xhr.status})`)),
          );
          xhr.addEventListener("error", () =>
            reject(new Error("Network error — check your connection")),
          );
          xhr.addEventListener("abort", () =>
            reject(
              new Error(
                stalled.current
                  ? "Upload stalled — no progress for 90 seconds."
                  : "Upload cancelled",
              ),
            ),
          );
          xhr.open("PUT", uploadUrl);
          xhr.setRequestHeader("Content-Type", contentType);
          xhr.send(file);
        });
        xhrRef.current = null;
        lastActivity.current = null;

        setUpload({ file, progress: 100, status: "finishing" });
        const done = await fetch(`/api/songs/${songId}/complete`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(duration ? { durationSecs: duration } : {}),
        });
        if (!done.ok) {
          const body = await done.json().catch(() => ({}));
          throw new Error(body.error ?? "Failed to confirm upload");
        }
        setUpload(null);
        await load();
      } catch (err) {
        xhrRef.current = null;
        lastActivity.current = null;
        // Drop the half-registered song so a retry starts clean.
        if (songId) {
          fetch(`/api/songs/${songId}`, { method: "DELETE" }).catch(() => {});
        }
        setUpload({
          file,
          progress: 0,
          status: "error",
          error: err instanceof Error ? err.message : "Upload failed",
        });
      }
    },
    [projectId, load],
  );

  async function removeSong(song: Song) {
    if (
      !window.confirm(
        `Delete "${song.filename ?? "this song"}"? Cuts using it as their music bed lose the bed.`,
      )
    ) {
      return;
    }
    setBusy(song.id);
    await fetch(`/api/songs/${song.id}`, { method: "DELETE" }).catch(() => {});
    setBusy(null);
    load();
  }

  async function resync(song: Song) {
    setBusy(song.id);
    await fetch(`/api/songs/${song.id}/sync`, { method: "POST" }).catch(() => {});
    setBusy(null);
    load();
  }

  const uploading = upload != null && upload.status !== "error";

  return (
    <section className="flex flex-col gap-3">
      <h2 className="text-sm font-semibold text-zinc-700 dark:text-zinc-300">
        Songs
      </h2>

      <input
        ref={fileRef}
        type="file"
        accept={ACCEPT}
        className="hidden"
        onChange={(e) => {
          const f = e.target.files?.[0];
          if (f) uploadFile(f);
          e.target.value = "";
        }}
      />
      <button
        onClick={() => fileRef.current?.click()}
        disabled={uploading}
        className="flex items-center justify-center gap-2 rounded-xl border-2 border-dashed border-zinc-300 py-3 text-sm text-zinc-600 dark:border-zinc-700 dark:text-zinc-400 disabled:opacity-50"
      >
        <span className="text-lg">♪</span>
        {uploading ? "Uploading…" : "Upload song / audio master"}
      </button>

      {upload && (
        <div className="flex flex-col gap-1.5 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900">
          <div className="flex items-center justify-between gap-2 text-sm">
            <span className="truncate">{upload.file.name}</span>
            <span className="shrink-0 text-xs text-zinc-500">
              {upload.status === "preparing" && "Preparing…"}
              {upload.status === "uploading" && `${upload.progress}%`}
              {upload.status === "finishing" && "Syncing to clips…"}
              {upload.status === "error" && <span className="text-red-500">Failed</span>}
            </span>
          </div>
          {upload.status === "uploading" && (
            <div className="h-1 w-full overflow-hidden rounded-full bg-zinc-200 dark:bg-zinc-700">
              <div
                className="h-full rounded-full bg-zinc-900 transition-all duration-150 dark:bg-white"
                style={{ width: `${upload.progress}%` }}
              />
            </div>
          )}
          {upload.status === "error" && (
            <>
              <p className="text-xs text-red-500">{upload.error}</p>
              <div className="flex gap-3">
                <button
                  onClick={() => uploadFile(upload.file)}
                  className="text-xs font-semibold text-blue-600 dark:text-blue-400"
                >
                  Retry
                </button>
                <button
                  onClick={() => setUpload(null)}
                  className="text-xs text-zinc-400 hover:text-zinc-600 dark:hover:text-zinc-300"
                >
                  Dismiss
                </button>
              </div>
            </>
          )}
        </div>
      )}

      {loadError && <p className="text-xs text-red-500">{loadError}</p>}

      {songs && songs.length > 0 && (
        <ul className="flex flex-col gap-2">
          {songs.map((s) => (
            <li
              key={s.id}
              className="flex flex-col gap-2 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
            >
              <div className="flex items-center justify-between gap-2">
                <span className="truncate text-sm text-zinc-800 dark:text-zinc-200">
                  {s.filename ?? "Untitled"}
                </span>
                <span className="shrink-0 text-xs text-zinc-500">
                  {fmtDuration(s.duration_secs)} ·{" "}
                  {s.status === "ready" ? (
                    <span className="text-green-600 dark:text-green-400">ready</span>
                  ) : (
                    s.status
                  )}
                </span>
              </div>
              {s.clips.length > 0 && (
                <ul className="flex flex-col gap-1">
                  {s.clips.map((c) => (
                    <ClipSyncRow key={c.clip_id} c={c} />
                  ))}
                </ul>
              )}
              <div className="flex gap-3">
                {s.status === "ready" && (
                  <button
                    onClick={() => resync(s)}
                    disabled={busy === s.id}
                    className="text-xs font-semibold text-blue-600 disabled:opacity-50 dark:text-blue-400"
                  >
                    {s.clips.length ? "Re-sync clips" : "Sync clips"}
                  </button>
                )}
                <button
                  onClick={() => removeSong(s)}
                  disabled={busy === s.id}
                  className="text-xs text-zinc-400 hover:text-red-500 disabled:opacity-50"
                >
                  Delete
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}
    </section>
  );
}
