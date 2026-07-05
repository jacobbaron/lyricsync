import { NextResponse } from "next/server";
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
// Query params:
//   q      — the search text (required)
//   limit  — max hits (default 20, capped at 50)
//
// Ranking is deliberately naive keyword matching (no index / embeddings yet);
// the FTS index (S2), semantic search (S3), and hybrid ranking (S5) are the
// follow-ups. API-key callable (Authorization: Bearer lsk_...).

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

type HitKind = "transcript" | "visual_description" | "highlight";

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
