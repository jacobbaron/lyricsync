import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/projects/[id]/embed-coverage ───────────────────────────────────
// SEARCH S4 (#121): reports clip-embedding coverage for a project — how many
// "real" clips (uploaded — r2_key set) have a pooled clip_embeddings row vs.
// how many are still missing, so recall gaps in semantic search (S3) are
// queryable rather than invisible.
//
// Standalone stopgap: #113's unified `GET /api/projects/{id}/perception`
// coverage endpoint (which would report quality/motion/embedding/etc.
// together) doesn't exist yet. Fold this into it once #113 ships.
//
// API-key callable (Authorization: Bearer lsk_...).
// Response: { total_clips, embedded, missing: [clip_id, ...] }

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

  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  const { data: clips, error: clipsError } = await supabase
    .from("clips")
    .select("id")
    .eq("project_id", projectId)
    .not("r2_key", "is", null);
  if (clipsError) {
    return NextResponse.json({ error: clipsError.message }, { status: 500 });
  }

  const clipIds = (clips ?? []).map((c) => c.id as string);
  if (clipIds.length === 0) {
    return NextResponse.json({ total_clips: 0, embedded: 0, missing: [] });
  }

  // A pooled (t IS NULL) clip_embeddings row is the ground-truth signal that a
  // clip is actually indexed for semantic search.
  const { data: pooledRows, error: pooledError } = await supabase
    .from("clip_embeddings")
    .select("clip_id")
    .in("clip_id", clipIds)
    .is("t", null);
  if (pooledError) {
    return NextResponse.json({ error: pooledError.message }, { status: 500 });
  }

  const doneSet = new Set((pooledRows ?? []).map((r) => r.clip_id as string));
  const missing = clipIds.filter((id) => !doneSet.has(id));

  return NextResponse.json({
    total_clips: clipIds.length,
    embedded: doneSet.size,
    missing,
  });
}
