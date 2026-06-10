import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/stories/[id]/revisions ────────────────────────────────────────
// Lists the story's timeline revision history (newest first, max 50), without
// the full timeline snapshots to keep the payload small. Reinstate one with
// POST /api/stories/[id]/edit { restore_revision: n }.

export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: storyId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data: story } = await supabase
    .from("stories")
    .select("id, timeline_revision")
    .eq("id", storyId)
    .maybeSingle();

  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  const { data: revisions, error } = await supabase
    .from("story_revisions")
    .select("revision, ops, created_at")
    .eq("story_id", storyId)
    .order("revision", { ascending: false })
    .limit(50);

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({
    current_revision: story.timeline_revision ?? 0,
    revisions: revisions ?? [],
  });
}
