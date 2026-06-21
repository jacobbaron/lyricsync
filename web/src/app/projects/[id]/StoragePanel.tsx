"use client";

import { useCallback, useEffect, useState } from "react";
import { useRouter } from "next/navigation";

// ── types ─────────────────────────────────────────────────────────────────

interface Category {
  bytes: number;
  count: number;
}

interface RenderItem {
  story_id: string;
  title: string | null;
  bytes: number;
  created_at: string | null;
}

interface StorageData {
  project_id: string;
  total_bytes: number;
  categories: {
    clips: Category;
    renders: Category;
    transcripts: Category;
    analyses: Category;
  };
  renders: RenderItem[];
}

// ── helpers ───────────────────────────────────────────────────────────────

function fmtBytes(bytes: number): string {
  if (bytes <= 0) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.min(
    units.length - 1,
    Math.floor(Math.log(bytes) / Math.log(1024)),
  );
  const value = bytes / Math.pow(1024, i);
  return `${value.toFixed(value >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
}

// ── component ─────────────────────────────────────────────────────────────

export function StoragePanel({ projectId }: { projectId: string }) {
  const router = useRouter();
  const [data, setData] = useState<StorageData | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const [deletingProject, setDeletingProject] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const res = await fetch(`/api/projects/${projectId}/storage`);
      const json = await res.json();
      if (!res.ok) {
        setError(json.error ?? "Failed to load storage usage");
        return;
      }
      setData(json as StorageData);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setLoading(false);
    }
  }, [projectId]);

  useEffect(() => {
    void load();
  }, [load]);

  async function deleteRender(storyId: string) {
    if (!confirm("Delete this rendered output? The cut can be re-rendered later."))
      return;
    setBusyId(storyId);
    setError(null);
    try {
      const res = await fetch(`/api/stories/${storyId}/render`, {
        method: "DELETE",
      });
      if (!res.ok && res.status !== 404) {
        const json = await res.json().catch(() => ({}));
        setError(json.error ?? "Failed to delete output");
        return;
      }
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
    } finally {
      setBusyId(null);
    }
  }

  async function deleteProject() {
    if (
      !confirm(
        "Permanently delete this entire project — all clips, transcripts and rendered cuts? This cannot be undone.",
      )
    )
      return;
    setDeletingProject(true);
    setError(null);
    try {
      const res = await fetch(`/api/projects/${projectId}`, {
        method: "DELETE",
      });
      if (!res.ok && res.status !== 404) {
        const json = await res.json().catch(() => ({}));
        setError(json.error ?? "Failed to delete project");
        setDeletingProject(false);
        return;
      }
      router.push("/");
      router.refresh();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Network error");
      setDeletingProject(false);
    }
  }

  const cats = data?.categories;
  const otherBytes =
    (cats?.transcripts.bytes ?? 0) + (cats?.analyses.bytes ?? 0);

  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900 px-4 py-4 flex flex-col gap-4">
      <div className="flex items-center justify-between">
        <h2 className="text-sm font-semibold text-zinc-900 dark:text-zinc-100">
          Storage
        </h2>
        {data && (
          <span className="text-xs font-mono text-zinc-500 dark:text-zinc-400">
            {fmtBytes(data.total_bytes)} total
          </span>
        )}
      </div>

      {loading && (
        <p className="text-xs text-zinc-400 dark:text-zinc-500">
          Calculating usage·
        </p>
      )}

      {error && <p className="text-xs text-red-500">{error}</p>}

      {data && cats && (
        <>
          {/* Category breakdown */}
          <div className="flex flex-col gap-1.5">
            <Row
              label="Source clips"
              count={cats.clips.count}
              bytes={cats.clips.bytes}
            />
            <Row
              label="Rendered cuts"
              count={cats.renders.count}
              bytes={cats.renders.bytes}
            />
            {otherBytes > 0 && (
              <Row
                label="Transcripts & analysis"
                count={cats.transcripts.count + cats.analyses.count}
                bytes={otherBytes}
              />
            )}
          </div>

          {/* Per-render cleanup list */}
          {data.renders.length > 0 && (
            <div className="flex flex-col gap-2 border-t border-zinc-100 dark:border-zinc-800 pt-3">
              <p className="text-xs font-medium text-zinc-400 dark:text-zinc-500">
                Rendered cuts — delete an output to free space (re-renderable)
              </p>
              {data.renders.map((r) => (
                <div
                  key={r.story_id}
                  className="flex items-center justify-between gap-2 rounded-lg border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/50 px-3 py-2"
                >
                  <div className="min-w-0">
                    <p className="text-xs font-medium text-zinc-700 dark:text-zinc-300 truncate">
                      {r.title || "Untitled cut"}
                    </p>
                    <p className="text-xs font-mono text-zinc-400">
                      {fmtBytes(r.bytes)}
                    </p>
                  </div>
                  <button
                    onClick={() => deleteRender(r.story_id)}
                    disabled={busyId === r.story_id}
                    className="shrink-0 rounded-lg border border-red-200 dark:border-red-900 px-2.5 py-1 text-xs font-semibold text-red-500 disabled:opacity-40 transition-colors hover:bg-red-50 dark:hover:bg-red-950"
                  >
                    {busyId === r.story_id ? "Deleting…" : "Delete output"}
                  </button>
                </div>
              ))}
            </div>
          )}

          {/* Whole-project deletion */}
          <div className="border-t border-zinc-100 dark:border-zinc-800 pt-3">
            <button
              onClick={deleteProject}
              disabled={deletingProject}
              className="w-full rounded-xl border border-red-200 dark:border-red-900 px-4 py-2 text-xs font-semibold text-red-500 disabled:opacity-40 transition-colors hover:bg-red-50 dark:hover:bg-red-950"
            >
              {deletingProject
                ? "Deleting project…"
                : "Delete entire project"}
            </button>
          </div>
        </>
      )}
    </section>
  );
}

function Row({
  label,
  count,
  bytes,
}: {
  label: string;
  count: number;
  bytes: number;
}) {
  return (
    <div className="flex items-center justify-between text-xs">
      <span className="text-zinc-500 dark:text-zinc-400">
        {label}
        <span className="text-zinc-400 dark:text-zinc-600"> · {count}</span>
      </span>
      <span className="font-mono text-zinc-700 dark:text-zinc-300">
        {fmtBytes(bytes)}
      </span>
    </div>
  );
}
