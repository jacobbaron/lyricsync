import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/projects/[id]/search?q=... ─────────────────────────────────────
// PERCEPTION T4: cross-clip semantic search within a project. Embeds the text
// query into the same CLIP space as the frames (via the Modal embed_text
// endpoint), then cosine-searches clip_embeddings through the
// search_clip_embeddings RPC (which runs under the caller's RLS, so results are
// scoped to the project owner).
//
// Query params:
//   q      — the search text (required)
//   limit  — max hits (default 10, capped at 50)
//   pooled — "1" to also match whole-clip pooled vectors (default: frames only)
//
// Response: { query, results: [{ clip_id, t, score }] } — t is null for a
// pooled clip-level hit. The clip_id/t pairs are directly usable to build a
// cross-project cut.
//
// API-key callable (Authorization: Bearer lsk_...).

export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const url = new URL(request.url);
  const q = (url.searchParams.get("q") ?? "").trim();
  if (!q) {
    return NextResponse.json({ error: "q (search text) is required" }, { status: 400 });
  }
  const limit = Math.min(
    Math.max(parseInt(url.searchParams.get("limit") ?? "10", 10) || 10, 1),
    50,
  );
  const framesOnly = url.searchParams.get("pooled") !== "1";

  // Ownership check (RLS): a project the caller can't see reads as not found.
  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Embed the query text via Modal (same CLIP model as the frames).
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

  // pgvector text literal for the RPC's ::vector cast.
  const queryLiteral = `[${embedding.join(",")}]`;

  const { data: results, error } = await supabase.rpc("search_clip_embeddings", {
    p_project_id: projectId,
    p_query: queryLiteral,
    p_match_count: limit,
    p_frames_only: framesOnly,
  });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ query: q, results: results ?? [] });
}
