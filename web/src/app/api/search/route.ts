import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";
import { resolveAuth } from "@/lib/auth/resolve";
import { getObjectText } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── GET /api/search?q=... ───────────────────────────────────────────────────
// SEARCH S1 (#83): cross-project library discovery. Finds clips across ALL of
// the caller's projects from a verbal query, over data we already have —
// transcripts (per-project merged.json), clips.visual_description, and
// visual_analyses highlights — and returns hits as
// { clip_id, project, timestamp, snippet } that drop straight into a
// cross-project cut (clip_id + clip-local timestamp). See
// docs/cross_project_search.md.
//
// SEARCH S3 (#120): `mode=semantic` reuses this same surface for library-wide
// *vector* search over CLIP embeddings (PERCEPTION T4, #87 — same table/model
// as the intra-project GET /api/projects/[id]/search, see
// docs/embeddings_search.md), generalized to drop the single-project scope
// via the search_clip_embeddings_global RPC (RLS/SECURITY INVOKER-scoped, so
// results never cross owners). Coverage is currently whatever clips already
// have embeddings — most of the library is unembedded until S4 (#121,
// auto-embed/backfill) ships, so don't expect exhaustive recall yet.
//
// Query params:
//   q      — the search text (required)
//   limit  — max hits (default 20, capped at 50)
//   mode   — "keyword" (default) or "semantic". Semantic mode additionally
//            accepts:
//     pooled — "1" to also match whole-clip pooled vectors (default: frames
//              only) — mirrors the intra-project route's `pooled` param.
//
// Ranking is deliberately naive keyword matching in the default mode (no FTS
// index yet — S2, #119); hybrid fusion of keyword + semantic is S5 (#122).
// API-key callable (Authorization: Bearer lsk_...).

interface ClipRow {
  id: string;
  project_id: string;
  filename: string | null;
  visual_description: string | null;
}

interface MergedWord {
  text?: string;
  local_start?: number;
  source?: string;
}

interface Highlight {
  time?: number | string;
  description?: string;
  kind?: string;
  expression?: string;
  tone?: string;
}

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

type HitKind = "transcript" | "visual_description" | "highlight" | "embedding";

interface Candidate {
  kind: HitKind;
  text: string;
  timestamp: number | null;
  snippet: string;
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
function normalizeToken(raw: string): string {
  return raw.toLowerCase().replace(/^[^a-z0-9']+|[^a-z0-9']+$/g, "");
}

function tokenize(text: string): string[] {
  return text
    .split(/\s+/)
    .map(normalizeToken)
    .filter((t) => t.length > 0);
}

// Naive keyword score for a candidate text against the query terms:
//   10 * (distinct terms matched) + (total occurrences) + 50 (whole-phrase hit).
function scoreText(text: string, terms: string[], phrase: string): number {
  if (!text) return 0;
  const tokens = tokenize(text);
  if (tokens.length === 0) return 0;
  const counts = new Map<string, number>();
  for (const tok of tokens) counts.set(tok, (counts.get(tok) ?? 0) + 1);

  let matchedTerms = 0;
  let occurrences = 0;
  for (const term of terms) {
    const c = counts.get(term) ?? 0;
    if (c > 0) {
      matchedTerms += 1;
      occurrences += c;
    }
  }
  if (matchedTerms === 0) return 0;

  let score = matchedTerms * 10 + occurrences;
  if (phrase.includes(" ") && text.toLowerCase().includes(phrase)) score += 50;
  return score;
}

// Best transcript window for a clip: score every word, then center a ±5-word
// snippet on the highest-scoring matched word (its local_start is the timestamp).
function transcriptCandidate(
  words: MergedWord[],
  terms: string[],
  phrase: string,
): Candidate | null {
  if (words.length === 0) return null;
  const termSet = new Set(terms);

  let bestIdx = -1;
  for (let i = 0; i < words.length; i += 1) {
    if (termSet.has(normalizeToken(words[i].text ?? ""))) {
      bestIdx = i;
      break;
    }
  }
  if (bestIdx === -1) return null;

  const joined = words.map((w) => w.text ?? "").join(" ");
  const score = scoreText(joined, terms, phrase);
  if (score === 0) return null;

  const from = Math.max(0, bestIdx - 5);
  const to = Math.min(words.length, bestIdx + 6);
  const snippet = words
    .slice(from, to)
    .map((w) => w.text ?? "")
    .join(" ")
    .trim();
  const ts = words[bestIdx].local_start;

  return {
    kind: "transcript",
    text: joined,
    timestamp: typeof ts === "number" && Number.isFinite(ts) ? ts : null,
    snippet,
  };
}

interface EmbeddingHitRow {
  clip_id: string;
  project_id: string;
  t: number | null;
  score: number;
}

// mode=semantic: library-wide vector search over clip_embeddings (SEARCH S3,
// #120). Embeds `q` via the same Modal embed_text endpoint the intra-project
// GET /api/projects/[id]/search route uses, then cosine-searches through the
// search_clip_embeddings_global RPC — the cross-project generalization of
// search_clip_embeddings (PERCEPTION T4, #87), scoped by the caller's RLS
// (SECURITY INVOKER) rather than a project id. Results are mapped into this
// route's usual { clip_id, project, project_id, filename, kind, timestamp,
// snippet, score } shape so callers don't need mode-specific parsing.
async function semanticSearch(
  supabase: SupabaseClient,
  q: string,
  limit: number,
  framesOnly: boolean,
): Promise<NextResponse> {
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
      p_match_count: limit,
      p_frames_only: framesOnly,
    },
  );
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const rows = (rpcResults ?? []) as EmbeddingHitRow[];
  if (rows.length === 0) {
    return NextResponse.json({ query: q, terms: [], count: 0, results: [] });
  }

  // The RPC only returns ids — look up filenames/project names for the
  // shared response shape (mirrors how the keyword path attaches names).
  const clipIds = Array.from(new Set(rows.map((r) => r.clip_id)));
  const projectIds = Array.from(new Set(rows.map((r) => r.project_id)));

  const [clipRes, projRes] = await Promise.all([
    supabase.from("clips").select("id, filename").in("id", clipIds),
    supabase.from("projects").select("id, name").in("id", projectIds),
  ]);
  if (clipRes.error) {
    return NextResponse.json({ error: clipRes.error.message }, { status: 500 });
  }
  if (projRes.error) {
    return NextResponse.json({ error: projRes.error.message }, { status: 500 });
  }

  const filenameByClip = new Map<string, string | null>(
    (clipRes.data ?? []).map((c: { id: string; filename: string | null }) => [
      c.id,
      c.filename,
    ]),
  );
  const projectNameById = new Map<string, string>(
    (projRes.data ?? []).map((p: { id: string; name: string }) => [p.id, p.name]),
  );

  const results: SearchHit[] = rows.map((r) => ({
    clip_id: r.clip_id,
    project: projectNameById.get(r.project_id) ?? null,
    project_id: r.project_id,
    filename: filenameByClip.get(r.clip_id) ?? null,
    kind: "embedding",
    timestamp: r.t,
    snippet:
      r.t != null
        ? `frame @ ${r.t.toFixed(1)}s (semantic match)`
        : "whole-clip semantic match",
    score: r.score,
  }));

  return NextResponse.json({ query: q, terms: [], count: results.length, results });
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
  if (mode === "semantic") {
    const framesOnly = url.searchParams.get("pooled") !== "1";
    return semanticSearch(supabase, q, limit, framesOnly);
  }

  const terms = Array.from(new Set(tokenize(q)));
  if (terms.length === 0) {
    return NextResponse.json({ error: "q has no searchable terms" }, { status: 400 });
  }
  const phrase = q.toLowerCase();

  // Clips + project names (RLS → only the caller's, across ALL projects).
  // Paginated so libraries larger than the PostgREST row cap aren't truncated.
  const [clipRes, projRes] = await Promise.all([
    fetchAllRows<ClipRow>((from, to) =>
      supabase
        .from("clips")
        .select("id, project_id, filename, visual_description")
        .range(from, to),
    ),
    fetchAllRows<{ id: string; name: string }>((from, to) =>
      supabase.from("projects").select("id, name").range(from, to),
    ),
  ]);
  if (clipRes.error) return NextResponse.json({ error: clipRes.error }, { status: 500 });
  if (projRes.error) return NextResponse.json({ error: projRes.error }, { status: 500 });

  const clips = clipRes.rows;
  const projectName = new Map<string, string>(
    projRes.rows.map((p) => [p.id, p.name]),
  );

  if (clips.length === 0) {
    return NextResponse.json({ query: q, terms, count: 0, results: [] });
  }

  // (project_id, filename) → clip_id, to attribute transcript words to a clip
  // without cross-project filename collisions.
  const clipByProjectFile = new Map<string, string>();
  for (const c of clips) {
    if (c.filename) clipByProjectFile.set(`${c.project_id}\u0000${c.filename}`, c.id);
  }

  // Highlights: newest `done` analysis per clip that carries highlights.
  // Paginated (multiple variants per clip can exceed the row cap on their own).
  const vaRes = await fetchAllRows<{ clip_id: string; result: unknown }>((from, to) =>
    supabase
      .from("visual_analyses")
      .select("clip_id, result, created_at")
      .eq("status", "done")
      .order("created_at", { ascending: false })
      .range(from, to),
  );
  if (vaRes.error) return NextResponse.json({ error: vaRes.error }, { status: 500 });

  const highlightsByClip = new Map<string, Highlight[]>();
  for (const row of vaRes.rows) {
    if (highlightsByClip.has(row.clip_id)) continue; // newest wins (ordered desc)
    const result = row.result as { highlights?: unknown } | null;
    const hs = result?.highlights;
    if (Array.isArray(hs) && hs.length > 0) {
      highlightsByClip.set(row.clip_id, hs as Highlight[]);
    }
  }

  // Transcript words per clip, read once per project from R2 (best-effort).
  const projectIds = Array.from(new Set(clips.map((c) => c.project_id)));
  const wordsByClip = new Map<string, MergedWord[]>();
  await Promise.all(
    projectIds.map(async (pid) => {
      try {
        const text = await getObjectText(`projects/${pid}/merged.json`);
        const merged = JSON.parse(text) as { words?: MergedWord[] };
        for (const w of merged.words ?? []) {
          if (!w.source) continue;
          const clipId = clipByProjectFile.get(`${pid}\u0000${w.source}`);
          if (!clipId) continue;
          const arr = wordsByClip.get(clipId);
          if (arr) arr.push(w);
          else wordsByClip.set(clipId, [w]);
        }
      } catch {
        // No transcript for this project yet — skip.
      }
    }),
  );

  // Best-scoring candidate per clip → one hit per clip.
  const hits: SearchHit[] = [];
  for (const clip of clips) {
    const candidates: Candidate[] = [];

    const tc = transcriptCandidate(wordsByClip.get(clip.id) ?? [], terms, phrase);
    if (tc) candidates.push(tc);

    if (clip.visual_description) {
      candidates.push({
        kind: "visual_description",
        text: clip.visual_description,
        timestamp: null,
        snippet: clip.visual_description.trim(),
      });
    }

    for (const h of highlightsByClip.get(clip.id) ?? []) {
      const desc = h.description;
      if (!desc) continue;
      const t = typeof h.time === "string" ? Number(h.time) : h.time;
      candidates.push({
        kind: "highlight",
        text: desc,
        timestamp: typeof t === "number" && Number.isFinite(t) ? t : null,
        snippet: desc.trim(),
      });
    }

    let best: { cand: Candidate; score: number } | null = null;
    for (const cand of candidates) {
      const score = scoreText(cand.text, terms, phrase);
      if (score > 0 && (!best || score > best.score)) best = { cand, score };
    }
    if (!best) continue;

    hits.push({
      clip_id: clip.id,
      project: projectName.get(clip.project_id) ?? null,
      project_id: clip.project_id,
      filename: clip.filename,
      kind: best.cand.kind,
      timestamp: best.cand.timestamp,
      snippet: best.cand.snippet,
      score: best.score,
    });
  }

  hits.sort((a, b) => b.score - a.score);
  const results = hits.slice(0, limit);

  return NextResponse.json({ query: q, terms, count: results.length, results });
}
