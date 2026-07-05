import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/search?q=... ───────────────────────────────────────────────────
// SEARCH S2 (#119): cross-project library discovery, now backed by a
// Postgres full-text index instead of the S1 (#83) in-memory naive scan.
// Finds clips across ALL of the caller's projects from a verbal query, over
// the same three sources as the MVP — transcripts, clips.visual_description,
// and visual_analyses highlights — and returns hits as
// { clip_id, project, timestamp, snippet } that drop straight into a
// cross-project cut (clip_id + clip-local timestamp). See
// docs/cross_project_search.md.
//
// Query params:
//   q      — the search text (required)
//   limit  — max hits (default 20, capped at 50)
//
// Ranking: a single Postgres RPC, search_library_fts() (see the S2
// migration), unions ts_rank_cd(·, websearch_to_tsquery(q)) scores over:
//   - clip_search_docs (transcript, windowed — the one source that lives in
//     R2 and needs a materialized/backfilled doc table, kept fresh by
//     POST /api/projects/[id]/merge)
//   - clips.visual_description_tsv (generated column, auto-fresh)
//   - visual_analyses.highlights_tsv (generated column, auto-fresh), unnested
//     per-highlight for exact timestamp/snippet attribution
// search_library_fts is a plain SQL function (SECURITY INVOKER by default),
// so it inherits the caller's RLS automatically — no explicit project/owner
// filter needed here, same as the MVP.

type HitKind = "transcript" | "visual_description" | "highlight";

interface FtsRow {
  clip_id: string;
  kind: HitKind;
  timestamp: number | null;
  snippet: string | null;
  score: number;
}

interface SearchHit {
  clip_id: string;
  project: string | null;
  project_id: string;
  filename: string | null;
  kind: HitKind;
  timestamp: number | null;
  snippet: string;
  score: number;
}

// Lowercase, strip surrounding punctuation, keep inner alphanumerics/'.
// Only used to populate the response's informational `terms` field now —
// ranking itself is entirely websearch_to_tsquery's job.
function normalizeToken(raw: string): string {
  return raw.toLowerCase().replace(/^[^a-z0-9']+|[^a-z0-9']+$/g, "");
}

function tokenize(text: string): string[] {
  return text
    .split(/\s+/)
    .map(normalizeToken)
    .filter((t) => t.length > 0);
}

// How many raw (clip_id, kind) candidate rows to pull from the RPC before
// grouping down to one best hit per clip. Needs to comfortably exceed
// `limit` because a single clip can contribute up to 3 rows (transcript +
// description + highlight) and we only keep its best one — see the
// dedup step below. This is a bounded approximation (top-N-then-group,
// not a true "best hit per clip across the whole library" sort): a clip
// whose only matching candidate ranks outside this pool is missed. In
// practice, for `limit` capped at 50, a pool this size makes that
// vanishingly unlikely.
const RPC_POOL_LIMIT = 1000;

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
  const terms = Array.from(new Set(tokenize(q)));
  if (terms.length === 0) {
    return NextResponse.json({ error: "q has no searchable terms" }, { status: 400 });
  }

  const { data: rows, error: rpcError } = await supabase.rpc("search_library_fts", {
    p_query: q,
    p_limit: RPC_POOL_LIMIT,
  });
  if (rpcError) {
    return NextResponse.json({ error: rpcError.message }, { status: 500 });
  }
  const ftsRows = (rows ?? []) as FtsRow[];

  // Best-scoring row per clip → one hit per clip (mirrors the S1 MVP's
  // per-clip "best candidate wins" behavior).
  const bestByClip = new Map<string, FtsRow>();
  for (const row of ftsRows) {
    const current = bestByClip.get(row.clip_id);
    if (!current || row.score > current.score) bestByClip.set(row.clip_id, row);
  }

  const ranked = Array.from(bestByClip.values()).sort((a, b) => b.score - a.score);
  const top = ranked.slice(0, limit);

  if (top.length === 0) {
    return NextResponse.json({ query: q, terms, count: 0, results: [] });
  }

  // Resolve clip_id -> (project name, project_id, filename) for just the
  // winning clips — both queries bounded by `limit` (≤ 50 clips, and at most
  // that many distinct projects), well under PostgREST's row cap, so no
  // pagination loop needed here (unlike the MVP's full-library scan).
  const clipIds = top.map((r) => r.clip_id);
  const { data: clipRows, error: clipsError } = await supabase
    .from("clips")
    .select("id, project_id, filename")
    .in("id", clipIds);
  if (clipsError) return NextResponse.json({ error: clipsError.message }, { status: 500 });

  const clipMeta = new Map(
    (clipRows ?? []).map((c: { id: string; project_id: string; filename: string | null }) => [
      c.id,
      c,
    ]),
  );
  const projectIds = Array.from(new Set(Array.from(clipMeta.values()).map((c) => c.project_id)));
  const { data: projRows, error: projError } = await supabase
    .from("projects")
    .select("id, name")
    .in("id", projectIds);
  if (projError) return NextResponse.json({ error: projError.message }, { status: 500 });

  const projectName = new Map<string, string>(
    (projRows ?? []).map((p: { id: string; name: string }) => [p.id, p.name]),
  );

  const results: SearchHit[] = [];
  for (const row of top) {
    const clip = clipMeta.get(row.clip_id);
    if (!clip) continue; // RLS or a race (clip deleted) — drop rather than 500.
    results.push({
      clip_id: row.clip_id,
      project: projectName.get(clip.project_id) ?? null,
      project_id: clip.project_id,
      filename: clip.filename,
      kind: row.kind,
      timestamp: row.timestamp,
      snippet: (row.snippet ?? "").trim(),
      score: row.score,
    });
  }

  return NextResponse.json({ query: q, terms, count: results.length, results });
}
