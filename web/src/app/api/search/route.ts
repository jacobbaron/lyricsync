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
//   snippet, thumbnail_url?, score, sources[] } that drop straight into a
// cross-project cut (clip_id + clip-local timestamp). See
// docs/cross_project_search.md.
//
// This route is the consolidation of five [SEARCH] epic (#128) tickets that
// were built in parallel against the S1 (#83) MVP and are reconciled here:
//
//   - S2 (#119): a Postgres full-text index. `mode=keyword` calls
//     search_library_fts() (see
//     supabase/migrations/20260705231100_add_clip_search_docs.sql), which
//     ranks with ts_rank_cd over websearch_to_tsquery(q) across three sources
//     (clip_search_docs transcript windows, clips.visual_description,
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
//   - S5 (#122): `mode=hybrid` — now the DEFAULT — fuses the S2 keyword
//     channel and the S3 semantic channel with Reciprocal Rank Fusion (RRF,
//     k=60) into one ranked, deduped (one hit per clip_id) list. See
//     "Hybrid ranking (S5 #122)" below.
//
// ⚠️ BEHAVIOR CHANGE: the default mode used to be keyword-only; it is now
// hybrid. `mode=keyword` and `mode=semantic` still force the old single-
// channel behavior exactly (unchanged code paths) — pass one of those
// explicitly to opt out of hybrid.
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
//   - S5's hybrid mode does NOT reimplement ranking: it factors the keyword
//     RPC+grouping logic out into `keywordCandidates()` and the semantic
//     embed+RPC+filter logic out into `semanticCandidates()`, calls both
//     (Promise.all — they're independent network calls, see hybridSearch()),
//     and fuses the two already-ranked, already-filtered per-clip lists with
//     RRF. `mode=keyword`/`mode=semantic` call the very same two helpers, so
//     there is exactly one implementation of each channel's ranking, used by
//     both its solo mode and hybrid.
//
// Hybrid ranking (S5 #122):
//   `mode=hybrid` (the default) runs the keyword and semantic channels in
//   parallel, groups each to one best-scoring row per clip_id (semantic mode
//   alone does NOT do this — it can return multiple frames per clip — but
//   hybrid fusion needs one representative per clip to fuse against the
//   keyword channel's already-one-per-clip shape), then fuses by Reciprocal
//   Rank Fusion: score = Σ 1/(k + rank) over every channel the clip appears
//   in (rank is 1-based within that channel's own ranked list), k=60 (the
//   standard RRF constant — no query-specific tuning was needed to satisfy
//   the acceptance criteria below). A clip appearing in both channels sums
//   both contributions and gets `sources: ["keyword", "semantic"]`;  a
//   clip appearing in only one gets a single-element `sources`. The hit's
//   display fields (kind/timestamp/snippet) come from whichever channel
//   ranked the clip better (lower rank number) — "the best-ranked
//   representative" per the ticket. If the semantic channel errors (Modal
//   down, embed service misconfigured) hybrid degrades to keyword-only
//   ranking rather than failing the now-default search endpoint; a
//   `warnings` field on the response reports the degradation.
//
// Query params:
//   q             — the search text (required)
//   limit         — max hits (default 20, capped at 50)
//   mode          — "hybrid" (default, S5 #122) fuses keyword + semantic via
//                   RRF; "keyword" (S2, #119) or "semantic" (S3, #120) force
//                   a single channel. Any other/unrecognized value (including
//                   no `mode` param at all) is treated as "hybrid" — mirrors
//                   the pre-S5 convention that an unrecognized mode fell back
//                   to whatever the default was, and the default is now
//                   hybrid instead of keyword.
//   pooled        — semantic mode only (also applies to hybrid's semantic
//                   channel): "1" to also match whole-clip pooled vectors
//                   (default: frames only).
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
//                   matches. In semantic mode (and hybrid's semantic
//                   channel), embedding hits are treated as "visual" (they're
//                   all image-side vectors) — "speech" short-circuits that
//                   channel to an empty result set without an embed call.
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

// ── S5 (#122): hybrid ranking ────────────────────────────────────────────────
// Which channel(s) contributed a hit. A hybrid hit found by both channels
// carries both; a keyword-mode or semantic-mode (solo) hit always carries
// exactly its one channel, kept for response-shape consistency across modes.
type HitSource = "keyword" | "semantic";

// Standard RRF constant (no query-specific tuning needed — see file-header
// "Hybrid ranking" note for why k=60 and not a weighted blend).
const RRF_K = 60;

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
  sources: HitSource[];
}

interface Facets {
  by_project: Record<string, number>;
  by_kind: Record<string, number>;
}

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

// ── S3 (#120): semantic channel ──────────────────────────────────────────────
//
// Library-wide vector search over clip_embeddings. Embeds `q` via the same
// Modal embed_text endpoint the intra-project GET /api/projects/[id]/search
// route uses, then cosine-searches through search_clip_embeddings_global —
// the cross-project generalization of search_clip_embeddings (PERCEPTION T4,
// #87), scoped by the caller's RLS (SECURITY INVOKER) rather than a project
// id. Factored out of the `mode=semantic` response builder so S5's hybrid
// mode can call the exact same channel logic (see hybridSearch below) instead
// of re-implementing it.
//
// Returns `{ rows }` (possibly empty — a `kind=speech` short-circuit or zero
// embedding matches are both success, not failure) or `{ error, status }` for
// a hard failure (missing config / embed call / RPC). Rows are pre-filtered
// (S7) but NOT deduped to one-per-clip — semantic mode alone intentionally
// allows multiple frame hits per clip; callers that need one-per-clip (hybrid
// fusion) group these themselves.
type SemanticCandidatesResult =
  | { rows: EmbeddingRow[] }
  | { error: string; status: number };

async function semanticCandidates(
  supabase: SupabaseClient,
  q: string,
  framesOnly: boolean,
  clipById: Map<string, ClipMetaRow>,
  filters: ClipFilters,
  kindFilter: KindFilter,
): Promise<SemanticCandidatesResult> {
  // Embedding hits are all image-side vectors — there is no "speech" source
  // in semantic mode. Short-circuit before the embed call rather than
  // returning zero results the expensive way.
  if (kindFilter === "speech") {
    return { rows: [] };
  }

  const embedTextUrl =
    process.env.MODAL_EMBED_TEXT_URL ??
    "https://jacobbaron--lyricsync-embed-text.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    return { error: "MODAL_WEBHOOK_SECRET not configured", status: 503 };
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
      return { error: `Query embedding failed (${res.status})`, status: 502 };
    }
    const payload = (await res.json()) as { embedding?: number[] };
    if (!Array.isArray(payload.embedding)) {
      return { error: "Query embedding service returned no vector", status: 502 };
    }
    embedding = payload.embedding;
  } catch (err) {
    console.error("[search] query embedding request failed:", err);
    return { error: "Query embedding request failed", status: 502 };
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
    return { error: error.message, status: 500 };
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

  return { rows: filtered };
}

function semanticHitFromRow(
  r: EmbeddingRow,
  clip: ClipMetaRow,
  projectName: Map<string, string>,
  score: number,
  sources: HitSource[],
): SearchHit {
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
    score,
    sources,
  };
}

// `mode=semantic` response builder — thin wrapper around semanticCandidates()
// that formats its rows into the route's response shape. Multiple frame hits
// per clip are preserved (not deduped) — see semanticCandidates' doc comment.
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
  const candidates = await semanticCandidates(supabase, q, framesOnly, clipById, filters, kindFilter);
  if ("error" in candidates) {
    return NextResponse.json({ error: candidates.error }, { status: candidates.status });
  }
  const { rows: filtered } = candidates;

  const facets: Facets = { by_project: {}, by_kind: {} };
  for (const r of filtered) {
    const clip = clipById.get(r.clip_id)!;
    const projectKey = projectName.get(clip.project_id) ?? clip.project_id;
    facets.by_project[projectKey] = (facets.by_project[projectKey] ?? 0) + 1;
    facets.by_kind.embedding = (facets.by_kind.embedding ?? 0) + 1;
  }

  const top = filtered.slice(0, limit);
  if (top.length === 0) {
    return NextResponse.json({ query: q, terms: [], count: 0, results: [], facets, mode: "semantic" });
  }

  const results: SearchHit[] = top.map((r) =>
    semanticHitFromRow(r, clipById.get(r.clip_id)!, projectName, r.score, ["semantic"]),
  );

  if (wantThumbnails) {
    await enrichThumbnails(supabase, results);
  }

  return NextResponse.json({ query: q, terms: [], count: results.length, results, facets, mode: "semantic" });
}

// ── S2 (#119): keyword channel ───────────────────────────────────────────────
//
// Factored out of the `mode=keyword` response builder (below, in GET) so S5's
// hybrid mode can call the exact same channel logic. Returns the FTS RPC's
// rows grouped to one best-scoring row per clip_id (S7 filters + `kind` gate
// already applied), sorted by score desc — or `{ error, status }` on a hard
// failure (empty tokenized query / RPC error).
type KeywordCandidatesResult =
  | { terms: string[]; ranked: FtsRow[] }
  | { error: string; status: number };

async function keywordCandidates(
  supabase: SupabaseClient,
  q: string,
  clipById: Map<string, ClipMetaRow>,
  filters: ClipFilters,
  kindFilter: KindFilter,
): Promise<KeywordCandidatesResult> {
  const terms = Array.from(new Set(tokenize(q)));
  if (terms.length === 0) {
    return { error: "q has no searchable terms", status: 400 };
  }

  const { data: rows, error: rpcError } = await supabase.rpc("search_library_fts", {
    p_query: q,
    p_limit: CANDIDATE_POOL_LIMIT,
  });
  if (rpcError) {
    return { error: rpcError.message, status: 500 };
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
    if (!clipPassesFilters(clip, filters)) continue;
    const current = bestByClip.get(row.clip_id);
    if (!current || row.score > current.score) bestByClip.set(row.clip_id, row);
  }

  const ranked = Array.from(bestByClip.values()).sort((a, b) => b.score - a.score);
  return { terms, ranked };
}

function keywordHitFromRow(
  row: FtsRow,
  clip: ClipMetaRow,
  projectName: Map<string, string>,
  score: number,
  sources: HitSource[],
): SearchHit {
  return {
    clip_id: row.clip_id,
    project: projectName.get(clip.project_id) ?? null,
    project_id: clip.project_id,
    filename: clip.filename,
    duration: normalizeDuration(clip.duration_secs),
    kind: row.kind,
    timestamp: row.timestamp,
    snippet: (row.snippet ?? "").trim(),
    score,
    sources,
  };
}

// ── S5 (#122): mode=hybrid (default) — RRF fusion ────────────────────────────
//
// Runs both channels in parallel (they're independent async calls — a simple
// Promise.all is enough, no need for more elaborate scheduling) and fuses
// with Reciprocal Rank Fusion. See the file-header "Hybrid ranking" note for
// the full design rationale (RRF choice/k, dedup key, representative-hit
// tie-break, semantic-failure degradation).
async function hybridSearch(
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
  const [kwResult, semResult] = await Promise.all([
    keywordCandidates(supabase, q, clipById, filters, kindFilter),
    semanticCandidates(supabase, q, framesOnly, clipById, filters, kindFilter),
  ]);

  // The keyword channel failing (bad query / DB error) is a hard failure for
  // hybrid too — matches the pre-S5 default's behavior for the same inputs
  // (it was keyword-only, so these exact conditions already 400/500'd).
  if ("error" in kwResult) {
    return NextResponse.json({ error: kwResult.error }, { status: kwResult.status });
  }
  const { terms, ranked: keywordRanked } = kwResult;

  // The semantic channel failing (Modal down, misconfigured) must NOT take
  // down what is now the default search endpoint — degrade to keyword-only
  // ranking and report it via `warnings` instead of erroring the request.
  const semanticRanked: EmbeddingRow[] = [];
  let semanticWarning: string | null = null;
  if ("error" in semResult) {
    console.error(
      `[search] hybrid: semantic channel unavailable (${semResult.status}): ${semResult.error}`,
    );
    semanticWarning = semResult.error;
  } else {
    // Group to one (best) row per clip for fusion purposes. semantic mode
    // alone allows multiple frames per clip, but hybrid's dedup key is
    // clip_id (see file header) — rows arrive pre-sorted best-first by the
    // RPC (order by embedding <=> query, i.e. best cosine similarity first),
    // so the first occurrence per clip_id is already its best frame.
    const seen = new Set<string>();
    for (const row of semResult.rows) {
      if (seen.has(row.clip_id)) continue;
      seen.add(row.clip_id);
      semanticRanked.push(row);
    }
  }

  const kwRankById = new Map<string, number>();
  keywordRanked.forEach((row, i) => kwRankById.set(row.clip_id, i + 1)); // 1-based rank
  const semRankById = new Map<string, number>();
  semanticRanked.forEach((row, i) => semRankById.set(row.clip_id, i + 1));

  const kwByClip = new Map(keywordRanked.map((r) => [r.clip_id, r]));
  const semByClip = new Map(semanticRanked.map((r) => [r.clip_id, r]));

  const allClipIds = new Set<string>([...kwRankById.keys(), ...semRankById.keys()]);

  interface Fused {
    clip_id: string;
    score: number;
    sources: HitSource[];
    useKeyword: boolean; // which channel's row supplies the display fields
  }
  const fused: Fused[] = [];
  for (const clipId of allClipIds) {
    const kwRank = kwRankById.get(clipId) ?? null;
    const semRank = semRankById.get(clipId) ?? null;

    let score = 0;
    const sources: HitSource[] = [];
    if (kwRank !== null) {
      score += 1 / (RRF_K + kwRank);
      sources.push("keyword");
    }
    if (semRank !== null) {
      score += 1 / (RRF_K + semRank);
      sources.push("semantic");
    }

    // "Keeping the best-ranked representative" (lower rank number = better).
    const useKeyword = kwRank !== null && (semRank === null || kwRank <= semRank);
    fused.push({ clip_id: clipId, score, sources, useKeyword });
  }

  fused.sort((a, b) => b.score - a.score);

  // Facets over the full fused, deduped (one row per clip_id), filtered
  // candidate set, before slicing to `limit` — same "tally what's in memory"
  // approach as the solo modes, just over the fused set instead of a single
  // channel's.
  const facets: Facets = { by_project: {}, by_kind: {} };
  for (const f of fused) {
    const clip = clipById.get(f.clip_id);
    if (!clip) continue;
    const projectKey = projectName.get(clip.project_id) ?? clip.project_id;
    facets.by_project[projectKey] = (facets.by_project[projectKey] ?? 0) + 1;
    const kind: HitKind = f.useKeyword ? kwByClip.get(f.clip_id)!.kind : "embedding";
    facets.by_kind[kind] = (facets.by_kind[kind] ?? 0) + 1;
  }

  const top = fused.slice(0, limit);
  const results: SearchHit[] = top.map((f) => {
    const clip = clipById.get(f.clip_id)!;
    if (f.useKeyword) {
      return keywordHitFromRow(kwByClip.get(f.clip_id)!, clip, projectName, f.score, f.sources);
    }
    return semanticHitFromRow(semByClip.get(f.clip_id)!, clip, projectName, f.score, f.sources);
  });

  if (wantThumbnails) {
    await enrichThumbnails(supabase, results);
  }

  const body: {
    query: string;
    terms: string[];
    count: number;
    results: SearchHit[];
    facets: Facets;
    mode: "hybrid";
    warnings?: string[];
  } = {
    query: q,
    terms,
    count: results.length,
    results,
    facets,
    mode: "hybrid",
  };
  if (semanticWarning) {
    body.warnings = [`semantic channel unavailable, degraded to keyword-only ranking: ${semanticWarning}`];
  }
  return NextResponse.json(body);
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
  // S5 (#122): default is now "hybrid" (was "keyword"). Any unrecognized
  // value — including no `mode` param at all — falls back to "hybrid",
  // mirroring the pre-S5 convention that an unrecognized mode fell back to
  // whatever the default was.
  const rawMode = (url.searchParams.get("mode") ?? "hybrid").toLowerCase();
  const mode = rawMode === "keyword" || rawMode === "semantic" ? rawMode : "hybrid";
  const wantThumbnails = url.searchParams.get("thumbnails") === "1";
  const framesOnly = url.searchParams.get("pooled") !== "1";

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

  if (mode === "hybrid") {
    return hybridSearch(
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

  // ── mode === "keyword": search_library_fts() (S2 #119) ────────────────────
  // This is the exact same code path the pre-S5 default used — explicit
  // `mode=keyword` is a byte-for-byte behavior override, not a new mode.
  const kwResult = await keywordCandidates(supabase, q, clipById, clipFilters, kindFilter);
  if ("error" in kwResult) {
    return NextResponse.json({ error: kwResult.error }, { status: kwResult.status });
  }
  const { terms, ranked } = kwResult;

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
    return NextResponse.json({ query: q, terms, count: 0, results: [], facets, mode: "keyword" });
  }

  const results: SearchHit[] = top.map((row) =>
    keywordHitFromRow(row, clipById.get(row.clip_id)!, projectName, row.score, ["keyword"]),
  );

  if (wantThumbnails) {
    await enrichThumbnails(supabase, results);
  }

  return NextResponse.json({ query: q, terms, count: results.length, results, facets, mode: "keyword" });
}
