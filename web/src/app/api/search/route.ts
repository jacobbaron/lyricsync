import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";
import { resolveAuth } from "@/lib/auth/resolve";
import { presignDownload } from "@/lib/r2/client";
import {
  cacheKey,
  callPerception,
  readCache,
  writeCache,
} from "@/lib/perception/modal";

export const runtime = "nodejs";

// ── GET /api/search?q=... ───────────────────────────────────────────────────
// Cross-project library discovery. Finds clips across ALL of the caller's
// projects from a verbal query and returns hits as
// { clip_id, project, project_id, filename, duration, kind, timestamp,
//   snippet, thumbnail_url?, score } that drop straight into a cross-project
// cut (clip_id + clip-local timestamp). See docs/cross_project_search.md.
//
// This route is the consolidation of four [SEARCH] epic (#128) tickets that
// were built in parallel against the S1 (#83) MVP and are reconciled here:
//
//   - S2 (#119): replaced the naive in-memory keyword scan with a Postgres
//     full-text index. `mode=keyword` (default) calls search_library_fts()
//     (see supabase/migrations/20260705231100_add_clip_search_docs.sql),
//     which ranks with ts_rank_cd over websearch_to_tsquery(q) across three
//     sources (clip_search_docs transcript windows, clips.visual_description,
//     visual_analyses highlights) and returns an already ts_headline-marked
//     `snippet` (see below).
//   - S3 (#120): `mode=semantic` — library-wide vector search over
//     clip_embeddings via search_clip_embeddings_global() (see
//     supabase/migrations/20260705230727_add_search_clip_embeddings_global.sql).
//   - S6 (#123): result enrichment — `duration` (clips.duration_secs) on
//     every hit, and an opt-in signed `thumbnail_url` (?thumbnails=1).
//   - S7 (#124): filters (`project`, `kind`, `min_duration`/`max_duration`,
//     `since`/`until`) and a `facets` field summarizing the (filtered,
//     pre-`limit`) candidate set.
//
// Reconciliation notes:
//   - S6 shipped JS-side `**term**` marker highlighting as an explicit
//     stand-in "until S2's FTS index lands." It has now landed: the
//     search_library_fts() SQL function itself calls Postgres `ts_headline`
//     (StartSel=**, StopSel=** — same marker convention S6 used) over the raw
//     source text, so keyword-mode snippets already arrive highlighted —
//     including stemmed matches a JS regex over literal terms would miss
//     (e.g. query "insulate" bolding "insulation"). The JS marker function is
//     gone; semantic-mode snippets are synthetic ("frame @ Xs...") and were
//     never highlighted in either version.
//   - S7's filter pass (`clipPassesFilters`) was written against the S1
//     in-memory clip list. It's unchanged in spirit but now runs against the
//     FTS RPC's *output* (grouped to one candidate per clip) instead of
//     against S1's raw per-clip candidate generation — see the file-local
//     comment above clipPassesFilters. `kind` gates which FTS row kinds are
//     eligible before the per-clip "best row wins" grouping (mirrors S7's
//     "gate candidate generation, not exclude the clip" original design).
//   - S3's early-return `semanticSearch` branch is preserved, but now shares
//     the single up-front clips/projects fetch (added for S7's filters) for
//     filename/project-name/duration/date lookups instead of doing its own
//     second round-trip, and gets S6's duration/thumbnail treatment too.
//
// Query params:
//   q             — the search text (required)
//   limit         — max hits (default 20, capped at 50)
//   mode          — "keyword" (default) or "semantic" (S3, #120). Any other
//                   value is treated as "keyword" (matches S3's shipped
//                   behavior — unrecognized modes aren't a 400).
//   pooled        — semantic mode only: "1" to also match whole-clip pooled
//                   vectors (default: frames only).
//   thumbnails    — "1" to include a signed thumbnail_url per result (S6,
//                   opt-in — costs one Modal frame-extraction call per
//                   result, subject to the same clip_inspections cache as
//                   /api/clips/{id}/frames).
//   project       — restrict to one/several projects; repeatable
//                   (?project=a&project=b) or comma-separated (?project=a,b).
//                   Each token matches a project id (uuid) OR project name
//                   (case-insensitive). Omit for all of the caller's
//                   projects. Unresolvable tokens simply match nothing.
//   kind          — "speech" | "visual" | "both" (default "both"). "speech"
//                   restricts keyword-mode hits to transcript matches;
//                   "visual" restricts to visual_description/highlight
//                   matches. In semantic mode, embedding hits are treated as
//                   "visual" (they're all image-side vectors) — "speech"
//                   short-circuits to an empty result set without an embed
//                   call.
//   min_duration  — minimum clip length in seconds (clips.duration_secs).
//                   Clips with unknown duration are excluded when set.
//   max_duration  — maximum clip length in seconds. Same null-handling.
//   since / until — ISO date/timestamp bounds on clip recording/creation
//                   time (clips.recorded_at, falling back to
//                   clips.created_at when recorded_at is null). Clips with
//                   neither are excluded when set.
//
// API-key callable (Authorization: Bearer lsk_...).

// PostgREST caps a single response at ~1000 rows, so every select must be
// paginated or larger libraries are silently truncated. Fetch all pages.
const PAGE_SIZE = 1000;

async function fetchAllRows<T>(
  query: (
    from: number,
    to: number,
  ) => PromiseLike<{ data: unknown[] | null; error: { message: string } | null }>,
): Promise<{ rows: T[]; error: string | null }> {
  const rows: T[] = [];
  for (let from = 0; ; from += PAGE_SIZE) {
    const { data, error } = await query(from, from + PAGE_SIZE - 1);
    if (error) return { rows, error: error.message };
    const batch = (data ?? []) as T[];
    rows.push(...batch);
    if (batch.length < PAGE_SIZE) break;
  }
  return { rows, error: null };
}

// How many raw (clip_id, kind) rows to pull from an RPC (search_library_fts
// or search_clip_embeddings_global) before grouping/filtering down to
// `limit`. Needs to comfortably exceed `limit` because (a) a single clip can
// contribute multiple FTS rows (transcript + description + highlight) and we
// only keep its best one, and (b) filters (project/kind/duration/date) are
// applied AFTER the RPC call, so the pool must be large enough to still have
// `limit` survivors post-filter. This is a bounded approximation — a hit
// whose only matching candidate ranks outside this pool is missed — not a
// true "best hit across the whole library" scan. In practice, for a library
// this size and `limit` capped at 50, a pool this size makes that vanishingly
// unlikely.
const CANDIDATE_POOL_LIMIT = 1000;

type HitKind = "transcript" | "visual_description" | "highlight" | "embedding";
type FtsHitKind = "transcript" | "visual_description" | "highlight";

interface FtsRow {
  clip_id: string;
  kind: FtsHitKind;
  timestamp: number | null;
  snippet: string | null;
  score: number;
}

interface EmbeddingRow {
  clip_id: string;
  project_id: string;
  t: number | null;
  score: number;
}

interface ClipMetaRow {
  id: string;
  project_id: string;
  filename: string | null;
  duration_secs: number | null;
  recorded_at: string | null;
  created_at: string | null;
}

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
}

interface Facets {
  by_project: Record<string, number>;
  by_kind: Record<string, number>;
}

const EMPTY_FACETS: Facets = { by_project: {}, by_kind: {} };

// Lowercase, strip surrounding punctuation, keep inner alphanumerics/'.
// Only used to populate the response's informational `terms` field —
// ranking itself is entirely websearch_to_tsquery's (keyword mode) or the
// embedding model's (semantic mode) job.
function normalizeToken(raw: string): string {
  return raw.toLowerCase().replace(/^[^a-z0-9']+|[^a-z0-9']+$/g, "");
}

function tokenize(text: string): string[] {
  return text
    .split(/\s+/)
    .map(normalizeToken)
    .filter((t) => t.length > 0);
}

// ── S7 (#124): filter params ────────────────────────────────────────────────

type KindFilter = "speech" | "visual" | "both";

interface ClipFilters {
  projectIds: Set<string> | null; // null = no project filter (all projects)
  minDuration: number | null;
  maxDuration: number | null;
  since: Date | null;
  until: Date | null;
}

// Collect a query param that may be repeated (?k=a&k=b) and/or
// comma-separated (?k=a,b) into a flat, trimmed, non-empty token list.
function collectListParam(url: URL, name: string): string[] {
  const out: string[] = [];
  for (const raw of url.searchParams.getAll(name)) {
    for (const part of raw.split(",")) {
      const t = part.trim();
      if (t) out.push(t);
    }
  }
  return out;
}

type ParamResult<T> = { value: T } | { error: string };

function parseKindFilter(raw: string | null): ParamResult<KindFilter> {
  if (raw === null || raw.trim() === "") return { value: "both" };
  const v = raw.trim().toLowerCase();
  if (v === "speech" || v === "visual" || v === "both") return { value: v };
  return { error: `kind must be one of "speech", "visual", "both" (got "${raw}")` };
}

function parseNonNegNumberParam(raw: string | null, name: string): ParamResult<number | null> {
  if (raw === null || raw.trim() === "") return { value: null };
  const n = Number(raw);
  if (!Number.isFinite(n) || n < 0) {
    return { error: `${name} must be a non-negative number (got "${raw}")` };
  }
  return { value: n };
}

function parseDateParam(raw: string | null, name: string): ParamResult<Date | null> {
  if (raw === null || raw.trim() === "") return { value: null };
  const d = new Date(raw);
  if (Number.isNaN(d.getTime())) {
    return { error: `${name} must be a valid date/timestamp (got "${raw}")` };
  }
  return { value: d };
}

// The filter pass: clip-level facts only (project, duration, recorded/created
// date). Applied to FTS/embedding candidates (after grouping to one per
// clip) — independent of q/terms so it composes with any scoring backend.
// `kind` is handled separately per-mode (see call sites) since it's about
// which *source* produced a hit, not a clip-level fact.
function clipPassesFilters(clip: ClipMetaRow, filters: ClipFilters): boolean {
  if (filters.projectIds && !filters.projectIds.has(clip.project_id)) return false;

  if (filters.minDuration !== null || filters.maxDuration !== null) {
    if (clip.duration_secs == null) return false;
    if (filters.minDuration !== null && clip.duration_secs < filters.minDuration) return false;
    if (filters.maxDuration !== null && clip.duration_secs > filters.maxDuration) return false;
  }

  if (filters.since !== null || filters.until !== null) {
    const dateStr = clip.recorded_at ?? clip.created_at;
    if (!dateStr) return false;
    const d = new Date(dateStr);
    if (Number.isNaN(d.getTime())) return false;
    if (filters.since !== null && d < filters.since) return false;
    if (filters.until !== null && d > filters.until) return false;
  }

  return true;
}

function normalizeDuration(secs: number | null): number | null {
  return typeof secs === "number" && Number.isFinite(secs) ? secs : null;
}

// ── S6 (#123): thumbnails ────────────────────────────────────────────────────

const THUMBNAIL_TTL = 3600;

// Signed thumbnail URL for a hit, reusing the /api/clips/{id}/frames
// perception tool IN-PROCESS (same callPerception/cache helpers that route
// uses) rather than an HTTP round-trip to our own API. `timestamp: null` hits
// (whole-clip visual_description matches) get a representative frame at t=0
// instead of no thumbnail. Best-effort: a Modal/ffmpeg failure here must not
// fail the search.
async function getThumbnailUrl(
  supabase: SupabaseClient,
  clipId: string,
  timestamp: number | null,
): Promise<string | null> {
  const t = Math.max(0, timestamp ?? 0);
  const params = { t, n: 1, interval: 1 };
  const key = cacheKey(clipId, "frames", params);

  try {
    const cached = await readCache(supabase, key);
    if (cached?.frames) {
      const items = cached.frames as { t: number; key: string }[];
      const first = items[0];
      return first ? await presignDownload(first.key, THUMBNAIL_TTL) : null;
    }

    const modalResult = await callPerception<{ frames: { t: number; key: string }[] }>(
      "frames",
      { clip_id: clipId, t, n: 1, interval: 1 },
    );
    await writeCache(supabase, clipId, "frames", key, params, {
      frames: modalResult.frames,
    });
    const first = modalResult.frames[0];
    return first ? await presignDownload(first.key, THUMBNAIL_TTL) : null;
  } catch {
    // Best-effort only — a Modal/ffmpeg failure on a thumbnail must not fail
    // the whole search response (PerceptionError or otherwise).
    return null;
  }
}

async function enrichThumbnails(supabase: SupabaseClient, results: SearchHit[]): Promise<void> {
  await Promise.all(
    results.map(async (hit) => {
      hit.thumbnail_url = await getThumbnailUrl(supabase, hit.clip_id, hit.timestamp);
    }),
  );
}

// ── S3 (#120): mode=semantic ─────────────────────────────────────────────────
//
// Library-wide vector search over clip_embeddings. Embeds `q` via the same
// Modal embed_text endpoint the intra-project GET /api/projects/[id]/search
// route uses, then cosine-searches through search_clip_embeddings_global —
// the cross-project generalization of search_clip_embeddings (PERCEPTION T4,
// #87), scoped by the caller's RLS (SECURITY INVOKER) rather than a project
// id. `clipById`/`projectName` (built once from the shared up-front fetch)
// supply filename/project-name/duration/date without a second round-trip.
async function semanticSearch(
  supabase: SupabaseClient,
  q: string,
  limit: number,
  framesOnly: boolean,
  wantThumbnails: boolean,
  clipById: Map<string, ClipMetaRow>,
  projectName: Map<string, string>,
  filters: ClipFilters,
  kindFilter: KindFilter,
): Promise<NextResponse> {
  // Embedding hits are all image-side vectors — there is no "speech" source
  // in semantic mode. Short-circuit before the embed call rather than
  // returning zero results the expensive way.
  if (kindFilter === "speech") {
    return NextResponse.json({ query: q, terms: [], count: 0, results: [], facets: EMPTY_FACETS });
  }

  const embedTextUrl =
    process.env.MODAL_EMBED_TEXT_URL ??
    "https://jacobbaron--lyricsync-embed-text.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    return NextResponse.json(
      { error: "MODAL_WEBHOOK_SECRET not configured" },
      { status: 503 },
    );
  }

  let embedding: number[];
  try {
    const res = await fetch(embedTextUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": webhookSecret,
      },
      body: JSON.stringify({ text: q }),
    });
    if (!res.ok) {
      const detail = await res.text().catch(() => "");
      console.error(`[search] embed_text returned ${res.status}: ${detail}`);
      return NextResponse.json(
        { error: `Query embedding failed (${res.status})` },
        { status: 502 },
      );
    }
    const payload = (await res.json()) as { embedding?: number[] };
    if (!Array.isArray(payload.embedding)) {
      return NextResponse.json(
        { error: "Query embedding service returned no vector" },
        { status: 502 },
      );
    }
    embedding = payload.embedding;
  } catch (err) {
    console.error("[search] query embedding request failed:", err);
    return NextResponse.json(
      { error: "Query embedding request failed" },
      { status: 502 },
    );
  }

  // pgvector text literal for the RPC's ::vector cast (same convention as the
  // intra-project route).
  const queryLiteral = `[${embedding.join(",")}]`;

  const { data: rpcResults, error } = await supabase.rpc(
    "search_clip_embeddings_global",
    {
      p_query: queryLiteral,
      p_match_count: CANDIDATE_POOL_LIMIT,
      p_frames_only: framesOnly,
    },
  );
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const rows = (rpcResults ?? []) as EmbeddingRow[];

  // S7 filters: project/duration/date, applied post-hoc against the shared
  // clip metadata map (kind is already handled by the short-circuit above —
  // every embedding row is implicitly "visual").
  const filtered = rows.filter((r) => {
    const clip = clipById.get(r.clip_id);
    if (!clip) return false; // RLS/race — drop rather than 500.
    return clipPassesFilters(clip, filters);
  });

  const facets: Facets = { by_project: {}, by_kind: {} };
  for (const r of filtered) {
    const clip = clipById.get(r.clip_id)!;
    const projectKey = projectName.get(clip.project_id) ?? clip.project_id;
    facets.by_project[projectKey] = (facets.by_project[projectKey] ?? 0) + 1;
    facets.by_kind.embedding = (facets.by_kind.embedding ?? 0) + 1;
  }

  const top = filtered.slice(0, limit);
  if (top.length === 0) {
    return NextResponse.json({ query: q, terms: [], count: 0, results: [], facets });
  }

  const results: SearchHit[] = top.map((r) => {
    const clip = clipById.get(r.clip_id)!;
    return {
      clip_id: r.clip_id,
      project: projectName.get(r.project_id) ?? null,
      project_id: r.project_id,
      filename: clip.filename,
      duration: normalizeDuration(clip.duration_secs),
      kind: "embedding",
      timestamp: r.t,
      snippet:
        r.t != null
          ? `frame @ ${r.t.toFixed(1)}s (semantic match)`
          : "whole-clip semantic match",
      score: r.score,
    };
  });

  if (wantThumbnails) {
    await enrichThumbnails(supabase, results);
  }

  return NextResponse.json({ query: q, terms: [], count: results.length, results, facets });
}

export async function GET(request: Request) {
  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const url = new URL(request.url);
  const q = (url.searchParams.get("q") ?? "").trim();
  if (!q) {
    return NextResponse.json(
      { error: "q (search text) is required" },
      { status: 400 },
    );
  }
  const limit = Math.min(
    Math.max(parseInt(url.searchParams.get("limit") ?? "20", 10) || 20, 1),
    50,
  );
  const mode = (url.searchParams.get("mode") ?? "keyword").toLowerCase();
  const wantThumbnails = url.searchParams.get("thumbnails") === "1";

  // ── Filter params (S7 #124) — validated for both modes ────────────────────
  const kindParsed = parseKindFilter(url.searchParams.get("kind"));
  if ("error" in kindParsed) return NextResponse.json({ error: kindParsed.error }, { status: 400 });
  const kindFilter = kindParsed.value;

  const minDurationParsed = parseNonNegNumberParam(
    url.searchParams.get("min_duration"),
    "min_duration",
  );
  if ("error" in minDurationParsed) {
    return NextResponse.json({ error: minDurationParsed.error }, { status: 400 });
  }
  const maxDurationParsed = parseNonNegNumberParam(
    url.searchParams.get("max_duration"),
    "max_duration",
  );
  if ("error" in maxDurationParsed) {
    return NextResponse.json({ error: maxDurationParsed.error }, { status: 400 });
  }
  if (
    minDurationParsed.value !== null &&
    maxDurationParsed.value !== null &&
    minDurationParsed.value > maxDurationParsed.value
  ) {
    return NextResponse.json(
      { error: "min_duration must be <= max_duration" },
      { status: 400 },
    );
  }

  const sinceParsed = parseDateParam(url.searchParams.get("since"), "since");
  if ("error" in sinceParsed) return NextResponse.json({ error: sinceParsed.error }, { status: 400 });
  const untilParsed = parseDateParam(url.searchParams.get("until"), "until");
  if ("error" in untilParsed) return NextResponse.json({ error: untilParsed.error }, { status: 400 });
  if (sinceParsed.value !== null && untilParsed.value !== null && sinceParsed.value > untilParsed.value) {
    return NextResponse.json({ error: "since must be <= until" }, { status: 400 });
  }

  const projectTokens = collectListParam(url, "project");

  // ── Shared lookups: clips + project names (RLS → only the caller's, across
  // ALL projects). Both modes need these for filename/project-name/duration/
  // date attachment and for resolving the `project` filter's name tokens, so
  // fetch once up front rather than each mode doing its own round-trip.
  const [clipRes, projRes] = await Promise.all([
    fetchAllRows<ClipMetaRow>((from, to) =>
      supabase
        .from("clips")
        .select("id, project_id, filename, duration_secs, recorded_at, created_at")
        .range(from, to),
    ),
    fetchAllRows<{ id: string; name: string }>((from, to) =>
      supabase.from("projects").select("id, name").range(from, to),
    ),
  ]);
  if (clipRes.error) return NextResponse.json({ error: clipRes.error }, { status: 500 });
  if (projRes.error) return NextResponse.json({ error: projRes.error }, { status: 500 });

  const clipById = new Map<string, ClipMetaRow>(clipRes.rows.map((c) => [c.id, c]));
  const projectName = new Map<string, string>(projRes.rows.map((p) => [p.id, p.name]));

  // Resolve `project` tokens (id or case-insensitive name) → project_ids.
  // Unresolvable tokens just don't match anything (no error) — narrowing to
  // zero results is a valid, if unhelpful, answer to a bad filter value.
  let projectFilterIds: Set<string> | null = null;
  if (projectTokens.length > 0) {
    const idSet = new Set(projRes.rows.map((p) => p.id));
    const nameToId = new Map(projRes.rows.map((p) => [p.name.toLowerCase(), p.id]));
    const resolved = new Set<string>();
    for (const tok of projectTokens) {
      if (idSet.has(tok)) resolved.add(tok);
      const byName = nameToId.get(tok.toLowerCase());
      if (byName) resolved.add(byName);
    }
    projectFilterIds = resolved;
  }

  const clipFilters: ClipFilters = {
    projectIds: projectFilterIds,
    minDuration: minDurationParsed.value,
    maxDuration: maxDurationParsed.value,
    since: sinceParsed.value,
    until: untilParsed.value,
  };

  if (mode === "semantic") {
    const framesOnly = url.searchParams.get("pooled") !== "1";
    return semanticSearch(
      supabase,
      q,
      limit,
      framesOnly,
      wantThumbnails,
      clipById,
      projectName,
      clipFilters,
      kindFilter,
    );
  }

  // ── Default: keyword mode via search_library_fts() (S2 #119) ──────────────
  const terms = Array.from(new Set(tokenize(q)));
  if (terms.length === 0) {
    return NextResponse.json({ error: "q has no searchable terms" }, { status: 400 });
  }

  const { data: rows, error: rpcError } = await supabase.rpc("search_library_fts", {
    p_query: q,
    p_limit: CANDIDATE_POOL_LIMIT,
  });
  if (rpcError) {
    return NextResponse.json({ error: rpcError.message }, { status: 500 });
  }
  const ftsRows = (rows ?? []) as FtsRow[];

  // `kind` gates which FTS row kinds are even eligible — mirrors S7's
  // original "gate candidate generation, not exclude the clip" design, now
  // applied to the RPC's output instead of in-process candidate generation.
  const wantSpeech = kindFilter !== "visual";
  const wantVisual = kindFilter !== "speech";
  function kindAllowed(k: FtsHitKind): boolean {
    if (k === "transcript") return wantSpeech;
    return wantVisual; // visual_description | highlight
  }

  // Best-scoring eligible row per clip → one hit per clip (mirrors the S1
  // MVP's per-clip "best candidate wins" behavior), with the S7 clip-level
  // filters (project/duration/date) applied at grouping time so both the
  // facets and the sliced `results` reflect the same filtered candidate set.
  const bestByClip = new Map<string, FtsRow>();
  for (const row of ftsRows) {
    if (!kindAllowed(row.kind)) continue;
    const clip = clipById.get(row.clip_id);
    if (!clip) continue; // RLS or a race (clip deleted) — drop rather than 500.
    if (!clipPassesFilters(clip, clipFilters)) continue;
    const current = bestByClip.get(row.clip_id);
    if (!current || row.score > current.score) bestByClip.set(row.clip_id, row);
  }

  const ranked = Array.from(bestByClip.values()).sort((a, b) => b.score - a.score);

  // Facets: counted over the full filtered candidate set, BEFORE slicing to
  // `limit` — no extra DB round-trips, just a tally over data already in
  // memory. by_project is keyed by project name (falling back to project_id
  // when a project has no name); by_kind uses the same kind vocabulary
  // returned per-hit.
  const facets: Facets = { by_project: {}, by_kind: {} };
  for (const row of ranked) {
    const clip = clipById.get(row.clip_id)!;
    const projectKey = projectName.get(clip.project_id) ?? clip.project_id;
    facets.by_project[projectKey] = (facets.by_project[projectKey] ?? 0) + 1;
    facets.by_kind[row.kind] = (facets.by_kind[row.kind] ?? 0) + 1;
  }

  const top = ranked.slice(0, limit);
  if (top.length === 0) {
    return NextResponse.json({ query: q, terms, count: 0, results: [], facets });
  }

  const results: SearchHit[] = top.map((row) => {
    const clip = clipById.get(row.clip_id)!;
    return {
      clip_id: row.clip_id,
      project: projectName.get(clip.project_id) ?? null,
      project_id: clip.project_id,
      filename: clip.filename,
      duration: normalizeDuration(clip.duration_secs),
      kind: row.kind,
      timestamp: row.timestamp,
      snippet: (row.snippet ?? "").trim(),
      score: row.score,
    };
  });

  if (wantThumbnails) {
    await enrichThumbnails(supabase, results);
  }

  return NextResponse.json({ query: q, terms, count: results.length, results, facets });
}
