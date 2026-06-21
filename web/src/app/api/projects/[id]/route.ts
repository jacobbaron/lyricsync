import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { deleteObjects } from "@/lib/r2/client";

export const runtime = "nodejs";

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

  const { data: project, error } = await supabase
    .from("projects")
    .select("id, name, status, error_message, created_at, clips(id, filename, status, error_message, duration_secs), stories(id, status, created_at)")
    .eq("id", projectId)
    .maybeSingle();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  return NextResponse.json(project);
}

// ── DELETE /api/projects/[id] ──────────────────────────────────────────────
// Permanently deletes a project: every R2 object it owns (source clips,
// transcripts, rendered cuts, visual-analysis results) is purged, then the
// project row is removed. The DB cascade (ON DELETE CASCADE) drops the child
// clips/stories/rounds/feedback/visual_analyses rows. This is irreversible.

export async function DELETE(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  // Confirm ownership (RLS scopes the row to the caller).
  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Collect every R2 key under this project before deleting the rows.
  const { data: clips } = await supabase
    .from("clips")
    .select("id, r2_key, transcript_r2_key")
    .eq("project_id", projectId);
  const { data: stories } = await supabase
    .from("stories")
    .select("render_r2_key")
    .eq("project_id", projectId);

  const clipIds = (clips ?? []).map((c) => c.id);
  let analyses: Array<{ result_r2_key: string | null }> = [];
  if (clipIds.length > 0) {
    const { data } = await supabase
      .from("visual_analyses")
      .select("result_r2_key")
      .in("clip_id", clipIds);
    analyses = data ?? [];
  }

  const keys: string[] = [];
  for (const c of clips ?? []) {
    if (c.r2_key) keys.push(c.r2_key);
    if (c.transcript_r2_key) keys.push(c.transcript_r2_key);
  }
  for (const s of stories ?? []) {
    if (s.render_r2_key) keys.push(s.render_r2_key);
  }
  for (const a of analyses) {
    if (a.result_r2_key) keys.push(a.result_r2_key);
  }

  // Purge R2 first; if this fails we keep the rows so nothing is orphaned.
  await deleteObjects(keys);

  const { error } = await supabase
    .from("projects")
    .delete()
    .eq("id", projectId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return new NextResponse(null, { status: 204 });
}
