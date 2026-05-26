import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/stories/[id] ──────────────────────────────────────────────────
// Returns story status, error_message, and (when done) render_r2_key.

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
    .select("id, project_id, status, error_message, render_r2_key, created_at")
    .eq("id", storyId)
    .maybeSingle();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  return NextResponse.json(story);
}
