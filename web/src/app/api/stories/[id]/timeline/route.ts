import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/stories/[id]/timeline ─────────────────────────────────────────
// Returns the story's editable timeline (EDL-01).
//
// timeline_json is null until the first edit materializes it (POST .../edit
// with ops: [] does exactly that); until then the render worker derives the
// timeline from ranges_json on the fly, so ranges_json is included for
// context.

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

  const { data: story, error } = await supabase
    .from("stories")
    .select("id, status, timeline_json, timeline_revision, ranges_json")
    .eq("id", storyId)
    .maybeSingle();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  return NextResponse.json({
    id: story.id,
    status: story.status,
    revision: story.timeline_revision ?? 0,
    timeline: story.timeline_json,
    ranges: story.ranges_json,
  });
}
