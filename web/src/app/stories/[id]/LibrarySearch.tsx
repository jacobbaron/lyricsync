"use client";

import { useCallback, useEffect, useId, useRef, useState } from "react";

// ── types ─────────────────────────────────────────────────────────────────
// Mirrors web/src/lib/openapi/existing.ts SearchHit / SearchResponse (S8
// #125 consumes the S1-S7/S9 GET /api/search shape — see
// web/src/app/api/search/route.ts for the authoritative current response).

type SearchMode = "hybrid" | "keyword" | "semantic";
type KindFilter = "speech" | "visual" | "both";
type HitKind = "transcript" | "visual_description" | "highlight" | "embedding";
type HitSource = "keyword" | "semantic";

interface SearchHit {
  clip_id: string;
  project: string | null;
  project_id: string;
  filename: string | null;
  duration: number | null;
  kind: HitKind;
  timestamp: number | null;
  snippet: string;
  thumbnail_url?: string | null;
  score: number;
  sources: HitSource[];
}

interface SearchResponse {
  query: string;
  count: number;
  results: SearchHit[];
  mode: SearchMode;
  warnings?: string[];
  error?: string;
}

interface ProjectOption {
  id: string;
  name: string;
}

interface TimelineVideoItem {
  id: string;
  kind: string;
  source?: string | null;
  clip_id?: string | null;
  src_start?: number | null;
  src_end?: number | null;
}

interface TimelineState {
  revision: number;
  items: TimelineVideoItem[];
  initialized: boolean; // false until timeline_json has ever been materialized
}

// ── constants ─────────────────────────────────────────────────────────────

const DEBOUNCE_MS = 400;
const DEFAULT_ADD_DURATION = 3; // seconds — no existing "add clip" flow in
// this app has a default (RangePicker/StoryCard require explicit start/end
// from the user); this is a reasonable short-highlight window around a
// search hit's timestamp, clamped to the clip's known duration.

// ── helpers ───────────────────────────────────────────────────────────────

function fmtTime(secs: number | null): string {
  if (secs == null || !Number.isFinite(secs)) return "—";
  const m = Math.floor(secs / 60);
  const s = Math.floor(secs % 60)
    .toString()
    .padStart(2, "0");
  return `${m}:${s}`;
}

function fmtDuration(secs: number | null): string {
  if (secs == null || !Number.isFinite(secs)) return "unknown length";
  if (secs < 60) return `${secs.toFixed(1)}s`;
  const m = Math.floor(secs / 60);
  const s = Math.round(secs % 60);
  if (s === 60) return `${m + 1}m 0s`;
  return `${m}m ${s}s`;
}

// Render a ts_headline-style snippet, turning **term** markers into real
// <strong> emphasis instead of showing literal asterisks. Semantic-mode
// snippets have no markers and pass through untouched.
function renderSnippet(snippet: string): React.ReactNode {
  const parts = snippet.split(/(\*\*[^*]+\*\*)/g);
  return parts.map((part, i) => {
    if (part.startsWith("**") && part.endsWith("**") && part.length > 4) {
      return <strong key={i}>{part.slice(2, -2)}</strong>;
    }
    return <span key={i}>{part}</span>;
  });
}

function sourceBadgeLabel(sources: HitSource[]): string {
  if (sources.includes("keyword") && sources.includes("semantic")) return "K+S";
  if (sources.includes("keyword")) return "K";
  if (sources.includes("semantic")) return "S";
  return "";
}

// Default [src_start, src_end] window for a clicked hit: a few seconds
// around the matched timestamp (or the clip start, for a whole-clip match),
// clamped to the clip's known duration when available.
function defaultWindow(hit: SearchHit): { src_start: number; src_end: number } {
  const dur = hit.duration ?? null;
  let start = Math.max(0, hit.timestamp ?? 0);
  let end = start + DEFAULT_ADD_DURATION;
  if (dur != null) {
    if (start >= dur) start = Math.max(0, dur - DEFAULT_ADD_DURATION);
    end = Math.min(start + DEFAULT_ADD_DURATION, dur);
    if (end <= start) end = Math.min(dur, start + 0.5);
  }
  return { src_start: start, src_end: end };
}

function findVideoItems(timeline: unknown): TimelineVideoItem[] {
  if (!timeline || typeof timeline !== "object") return [];
  const tracks = (timeline as { tracks?: unknown }).tracks;
  if (!Array.isArray(tracks)) return [];
  const videoTrack = tracks.find(
    (t): t is { type: string; items: TimelineVideoItem[] } =>
      !!t && typeof t === "object" && (t as { type?: string }).type === "video",
  );
  return videoTrack?.items ?? [];
}

// ── sub-components ────────────────────────────────────────────────────────

function ModeToggle({
  mode,
  onChange,
}: {
  mode: SearchMode;
  onChange: (m: SearchMode) => void;
}) {
  const options: { value: SearchMode; label: string }[] = [
    { value: "hybrid", label: "Hybrid" },
    { value: "keyword", label: "Keyword" },
    { value: "semantic", label: "Semantic" },
  ];
  return (
    <div className="flex rounded-lg border border-zinc-200 dark:border-zinc-700 overflow-hidden text-xs">
      {options.map((o) => (
        <button
          key={o.value}
          type="button"
          onClick={() => onChange(o.value)}
          className={[
            "px-2.5 py-1.5 font-medium transition-colors",
            mode === o.value
              ? "bg-blue-600 text-white"
              : "bg-white dark:bg-zinc-900 text-zinc-500 dark:text-zinc-400 hover:text-zinc-800 dark:hover:text-zinc-200",
          ].join(" ")}
        >
          {o.label}
        </button>
      ))}
    </div>
  );
}

function Thumbnail({ url, kind }: { url?: string | null; kind: HitKind }) {
  if (url) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- signed R2 URL, not in next/image's remotePatterns
      <img
        src={url}
        alt=""
        className="w-20 h-14 rounded-md object-cover bg-zinc-100 dark:bg-zinc-800 shrink-0"
      />
    );
  }
  return (
    <div className="w-20 h-14 rounded-md bg-zinc-100 dark:bg-zinc-800 flex items-center justify-center text-zinc-400 dark:text-zinc-600 text-xs shrink-0">
      {kind === "transcript" ? "💬" : kind === "embedding" ? "🔎" : "🎬"}
    </div>
  );
}

// ── main component ────────────────────────────────────────────────────────

interface Props {
  storyId: string;
}

export function LibrarySearch({ storyId }: Props) {
  const uid = useId();
  const [open, setOpen] = useState(false);

  // Search state
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<SearchMode>("hybrid");
  const [results, setResults] = useState<SearchHit[]>([]);
  const [searchWarnings, setSearchWarnings] = useState<string[]>([]);
  const [loading, setLoading] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);

  // Filters
  const [filtersOpen, setFiltersOpen] = useState(false);
  const [projects, setProjects] = useState<ProjectOption[]>([]);
  const [selectedProjects, setSelectedProjects] = useState<Set<string>>(new Set());
  const [kindFilter, setKindFilter] = useState<KindFilter>("both");
  const [minDuration, setMinDuration] = useState("");
  const [maxDuration, setMaxDuration] = useState("");
  const [since, setSince] = useState("");
  const [until, setUntil] = useState("");

  // Timeline / add-to-story state
  const [timeline, setTimeline] = useState<TimelineState | null>(null);
  const [addingId, setAddingId] = useState<string | null>(null);
  const [addedKeys, setAddedKeys] = useState<Set<string>>(new Set());
  const [addError, setAddError] = useState<string | null>(null);
  const [rendering, setRendering] = useState(false);

  const debounceRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Load the project list once (for the project filter) — same plain-fetch
  // pattern used throughout this app (no SWR/React Query in this codebase).
  useEffect(() => {
    fetch("/api/projects")
      .then((r) => (r.ok ? r.json() : []))
      .then((data) => setProjects(Array.isArray(data) ? data : []))
      .catch(() => {
        /* filter list is a nicety — a failed fetch just leaves it empty */
      });
  }, []);

  // Load the story's current timeline once, so "added" clips can be
  // reflected against a real base_revision (optimistic concurrency). Returns
  // the freshly fetched state (or null on failure) so callers that need the
  // just-fetched revision synchronously — e.g. the 409-retry path below —
  // don't have to rely on `timeline` state, which only updates on the next
  // render and would otherwise still read the stale value that just caused
  // the conflict.
  const loadTimeline = useCallback(async (): Promise<TimelineState | null> => {
    try {
      const res = await fetch(`/api/stories/${storyId}/timeline`);
      if (!res.ok) return null;
      const data = await res.json();
      const next: TimelineState = {
        revision: data.revision ?? 0,
        items: findVideoItems(data.timeline),
        initialized: data.timeline != null,
      };
      setTimeline(next);
      return next;
    } catch {
      // Best-effort — add-to-timeline still works without a cached revision;
      // the edit endpoint just applies against whatever's current.
      return null;
    }
  }, [storyId]);

  useEffect(() => {
    loadTimeline();
  }, [loadTimeline]);

  // Debounced search — fires on any change to the query or filters, only
  // while there's a non-empty query.
  useEffect(() => {
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const q = query.trim();
    if (!q) {
      setResults([]);
      setSearchError(null);
      setLoading(false);
      return;
    }
    setLoading(true);
    // AbortController guards against out-of-order responses: if the query or
    // filters change again before this fetch resolves, the effect cleanup
    // below aborts it, so a slower earlier response can never overwrite a
    // faster later one.
    const controller = new AbortController();
    debounceRef.current = setTimeout(async () => {
      const params = new URLSearchParams();
      params.set("q", q);
      params.set("mode", mode);
      params.set("thumbnails", "1");
      params.set("limit", "20");
      if (kindFilter !== "both") params.set("kind", kindFilter);
      for (const pid of selectedProjects) params.append("project", pid);
      if (minDuration.trim()) params.set("min_duration", minDuration.trim());
      if (maxDuration.trim()) params.set("max_duration", maxDuration.trim());
      if (since.trim()) params.set("since", since.trim());
      if (until.trim()) params.set("until", until.trim());

      try {
        const res = await fetch(`/api/search?${params.toString()}`, {
          signal: controller.signal,
        });
        const data: SearchResponse = await res.json();
        if (!res.ok) {
          setSearchError(data.error ?? `Search failed (${res.status})`);
          setResults([]);
          setSearchWarnings([]);
          return;
        }
        setSearchError(null);
        setResults(data.results ?? []);
        setSearchWarnings(data.warnings ?? []);
      } catch (err) {
        if (err instanceof DOMException && err.name === "AbortError") return;
        setSearchError(err instanceof Error ? err.message : "Network error");
        setResults([]);
      } finally {
        // Don't clear loading on behalf of a superseded request — an aborted
        // fetch's `finally` would otherwise flip loading back to false even
        // though a newer request (from a later keystroke) is still pending.
        if (!controller.signal.aborted) setLoading(false);
      }
    }, DEBOUNCE_MS);

    return () => {
      if (debounceRef.current) clearTimeout(debounceRef.current);
      controller.abort();
    };
  }, [query, mode, kindFilter, selectedProjects, minDuration, maxDuration, since, until]);

  const toggleProject = (id: string) => {
    setSelectedProjects((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  // Click-to-add: reuses the existing cross-project insert_clip authoring
  // path (POST /api/stories/[id]/edit — see docs/cross_project_editing.md
  // and docs/timeline_editing.md) rather than a new mechanism. On a 409
  // revision conflict, refetch the current revision and retry once.
  //
  // `revisionOverride` carries the freshly-fetched revision into the retry.
  // It must NOT come from `timeline?.revision` in the closure: setTimeline
  // inside loadTimeline() only takes effect on the *next* render, so the
  // synchronous recursive call below would otherwise still read the same
  // stale revision that just caused the 409 — a retry that can never
  // actually recover. Passing loadTimeline()'s return value explicitly
  // sidesteps that stale-closure trap.
  const addToTimeline = useCallback(
    async (
      hit: SearchHit,
      attempt = 0,
      revisionOverride?: number | null,
    ): Promise<void> => {
      const key = `${hit.clip_id}@${hit.timestamp ?? "whole"}`;
      setAddingId(key);
      setAddError(null);

      const { src_start, src_end } = defaultWindow(hit);
      const op = {
        op: "insert_clip",
        source: hit.filename ?? hit.project ?? "clip",
        clip_id: hit.clip_id,
        src_start,
        src_end,
      };

      const baseRevision =
        revisionOverride !== undefined ? revisionOverride : (timeline?.revision ?? null);

      try {
        const res = await fetch(`/api/stories/${storyId}/edit`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            ops: [op],
            base_revision: baseRevision,
          }),
        });
        const data = await res.json();

        if (res.status === 409 && attempt === 0) {
          const fresh = await loadTimeline();
          return addToTimeline(hit, attempt + 1, fresh?.revision ?? null);
        }
        if (!res.ok) {
          setAddError(data.detail ?? data.error ?? `Add failed (${res.status})`);
          return;
        }

        setTimeline({
          revision: data.revision,
          items: findVideoItems(data.timeline),
          initialized: true,
        });
        setAddedKeys((prev) => new Set(prev).add(key));
      } catch (err) {
        setAddError(err instanceof Error ? err.message : "Network error");
      } finally {
        setAddingId(null);
      }
    },
    [storyId, timeline?.revision, loadTimeline],
  );

  // Re-render the story from its current (edited) timeline — the existing
  // "empty body re-renders timeline_json in place" endpoint, so newly added
  // clips actually make it into the output. See docs/timeline_editing.md.
  const handleRerender = useCallback(async () => {
    setRendering(true);
    try {
      const res = await fetch(`/api/stories/${storyId}/render`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({}),
      });
      if (res.ok) {
        window.location.reload();
      }
    } finally {
      setRendering(false);
    }
  }, [storyId]);

  return (
    <section className="rounded-xl border border-zinc-200 dark:border-zinc-800 bg-white dark:bg-zinc-900">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        className="flex w-full items-center gap-1.5 px-4 py-3 group"
      >
        <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 group-hover:text-zinc-600 dark:group-hover:text-zinc-300 transition-colors">
          Search library
        </h2>
        <span className="text-xs text-zinc-400 dark:text-zinc-500">
          · search every project
        </span>
        <span className="ml-auto text-zinc-400 dark:text-zinc-500 text-xs">
          {open ? "▾" : "▸"}
        </span>
      </button>

      {open && (
        <div className="flex flex-col gap-4 px-4 pb-4">
          {/* Search box + mode toggle */}
          <div className="flex flex-col gap-2">
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={query}
                onChange={(e) => setQuery(e.target.value)}
                placeholder='Search clips across all projects — e.g. "ceiling", "someone laughing"'
                className="flex-1 min-w-0 rounded-lg border border-zinc-200 dark:border-zinc-700 px-3 py-2 text-sm bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-blue-500"
              />
              <ModeToggle mode={mode} onChange={setMode} />
            </div>

            <div className="flex items-center justify-between">
              <button
                type="button"
                onClick={() => setFiltersOpen((o) => !o)}
                className="text-xs font-medium text-zinc-400 dark:text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors"
              >
                {filtersOpen ? "▾" : "▸"} Filters
                {(selectedProjects.size > 0 ||
                  kindFilter !== "both" ||
                  minDuration ||
                  maxDuration ||
                  since ||
                  until) && (
                  <span className="ml-1 text-blue-500">(active)</span>
                )}
              </button>
              {timeline && (
                <span className="text-xs text-zinc-400 dark:text-zinc-500">
                  {timeline.items.length} clip{timeline.items.length === 1 ? "" : "s"} in timeline
                </span>
              )}
            </div>

            {filtersOpen && (
              <div className="flex flex-col gap-3 rounded-lg border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/50 px-3 py-3">
                {/* Kind */}
                <div className="flex flex-col gap-1">
                  <label className="text-xs text-zinc-500 dark:text-zinc-400">
                    Kind
                  </label>
                  <div className="flex gap-3">
                    {(["both", "speech", "visual"] as KindFilter[]).map((k) => (
                      <label
                        key={k}
                        className="flex items-center gap-1.5 text-xs text-zinc-600 dark:text-zinc-300"
                      >
                        <input
                          type="radio"
                          name={`${uid}-kind`}
                          checked={kindFilter === k}
                          onChange={() => setKindFilter(k)}
                        />
                        {k === "both" ? "Speech + visual" : k === "speech" ? "Speech" : "Visual"}
                      </label>
                    ))}
                  </div>
                </div>

                {/* Projects */}
                {projects.length > 0 && (
                  <div className="flex flex-col gap-1">
                    <label className="text-xs text-zinc-500 dark:text-zinc-400">
                      Projects
                      {selectedProjects.size === 0 && " (all)"}
                    </label>
                    <div className="flex flex-wrap gap-2 max-h-28 overflow-y-auto">
                      {projects.map((p) => (
                        <label
                          key={p.id}
                          className={[
                            "flex items-center gap-1.5 rounded-md border px-2 py-1 text-xs cursor-pointer transition-colors",
                            selectedProjects.has(p.id)
                              ? "border-blue-400 bg-blue-50 dark:bg-blue-950 text-blue-700 dark:text-blue-300"
                              : "border-zinc-200 dark:border-zinc-700 text-zinc-600 dark:text-zinc-300",
                          ].join(" ")}
                        >
                          <input
                            type="checkbox"
                            className="hidden"
                            checked={selectedProjects.has(p.id)}
                            onChange={() => toggleProject(p.id)}
                          />
                          {p.name}
                        </label>
                      ))}
                    </div>
                  </div>
                )}

                {/* Duration range */}
                <div className="grid grid-cols-2 gap-3">
                  <div className="flex flex-col gap-1">
                    <label className="text-xs text-zinc-500 dark:text-zinc-400">
                      Min duration (s)
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={minDuration}
                      onChange={(e) => setMinDuration(e.target.value)}
                      className="rounded-lg border border-zinc-200 dark:border-zinc-700 px-2 py-1.5 text-xs bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-xs text-zinc-500 dark:text-zinc-400">
                      Max duration (s)
                    </label>
                    <input
                      type="number"
                      min={0}
                      value={maxDuration}
                      onChange={(e) => setMaxDuration(e.target.value)}
                      className="rounded-lg border border-zinc-200 dark:border-zinc-700 px-2 py-1.5 text-xs bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                </div>

                {/* Date range */}
                <div className="grid grid-cols-2 gap-3">
                  <div className="flex flex-col gap-1">
                    <label className="text-xs text-zinc-500 dark:text-zinc-400">
                      Since
                    </label>
                    <input
                      type="date"
                      value={since}
                      onChange={(e) => setSince(e.target.value)}
                      className="rounded-lg border border-zinc-200 dark:border-zinc-700 px-2 py-1.5 text-xs bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                  <div className="flex flex-col gap-1">
                    <label className="text-xs text-zinc-500 dark:text-zinc-400">
                      Until
                    </label>
                    <input
                      type="date"
                      value={until}
                      onChange={(e) => setUntil(e.target.value)}
                      className="rounded-lg border border-zinc-200 dark:border-zinc-700 px-2 py-1.5 text-xs bg-white dark:bg-zinc-900 text-zinc-800 dark:text-zinc-200 focus:outline-none focus:ring-2 focus:ring-blue-500"
                    />
                  </div>
                </div>
              </div>
            )}
          </div>

          {/* Warnings (e.g. hybrid degraded to keyword-only) */}
          {searchWarnings.map((w, i) => (
            <p key={i} className="text-xs text-amber-500 dark:text-amber-400">
              ⚠ {w}
            </p>
          ))}

          {searchError && <p className="text-xs text-red-500">{searchError}</p>}

          {loading && (
            <p className="text-xs text-zinc-400 dark:text-zinc-500">Searching…</p>
          )}

          {!loading && query.trim() && !searchError && results.length === 0 && (
            <p className="text-xs text-zinc-400 dark:text-zinc-500">
              No results for &ldquo;{query.trim()}&rdquo;.
            </p>
          )}

          {/* Results list */}
          {results.length > 0 && (
            <ul className="flex flex-col gap-2">
              {results.map((hit) => {
                const key = `${hit.clip_id}@${hit.timestamp ?? "whole"}`;
                const isAdding = addingId === key;
                const isAdded = addedKeys.has(key);
                return (
                  <li
                    key={key}
                    className="flex gap-3 rounded-lg border border-zinc-100 dark:border-zinc-800 bg-zinc-50 dark:bg-zinc-800/50 px-3 py-2.5"
                  >
                    <Thumbnail url={hit.thumbnail_url} kind={hit.kind} />

                    <div className="flex flex-col gap-1 min-w-0 flex-1">
                      <div className="flex items-center gap-1.5 flex-wrap">
                        <span className="text-xs font-semibold text-zinc-700 dark:text-zinc-200 truncate">
                          {hit.project ?? "Unknown project"}
                        </span>
                        <span className="text-xs text-zinc-400 dark:text-zinc-500 truncate">
                          {hit.filename ?? ""}
                        </span>
                        {sourceBadgeLabel(hit.sources) && (
                          <span
                            title={`Matched by: ${hit.sources.join(", ")}`}
                            className="ml-auto shrink-0 rounded-full bg-zinc-200 dark:bg-zinc-700 px-1.5 py-0.5 text-[10px] font-mono text-zinc-500 dark:text-zinc-300"
                          >
                            {sourceBadgeLabel(hit.sources)}
                          </span>
                        )}
                      </div>

                      <p className="text-xs leading-relaxed text-zinc-600 dark:text-zinc-300 line-clamp-2">
                        {renderSnippet(hit.snippet)}
                      </p>

                      <div className="flex items-center gap-2 text-xs text-zinc-400 dark:text-zinc-500 font-mono">
                        <span>@{fmtTime(hit.timestamp)}</span>
                        <span>·</span>
                        <span>{fmtDuration(hit.duration)}</span>
                      </div>
                    </div>

                    <button
                      type="button"
                      onClick={() => addToTimeline(hit)}
                      disabled={isAdding || isAdded}
                      className={[
                        "self-center shrink-0 rounded-lg px-2.5 py-1.5 text-xs font-semibold transition-colors",
                        isAdded
                          ? "bg-green-100 dark:bg-green-950 text-green-700 dark:text-green-400"
                          : "bg-blue-600 hover:bg-blue-700 disabled:opacity-50 text-white",
                      ].join(" ")}
                    >
                      {isAdded ? "Added ✓" : isAdding ? "Adding…" : "+ Add"}
                    </button>
                  </li>
                );
              })}
            </ul>
          )}

          {addError && <p className="text-xs text-red-500">{addError}</p>}

          {addedKeys.size > 0 && (
            <button
              type="button"
              onClick={handleRerender}
              disabled={rendering}
              className="w-full rounded-xl border border-zinc-200 dark:border-zinc-700 bg-white dark:bg-zinc-900 hover:bg-zinc-50 dark:hover:bg-zinc-800 disabled:opacity-40 px-4 py-2.5 text-sm font-semibold text-zinc-800 dark:text-zinc-200 transition-colors"
            >
              {rendering ? "Starting render…" : "Render updated cut"}
            </button>
          )}
        </div>
      )}
    </section>
  );
}
