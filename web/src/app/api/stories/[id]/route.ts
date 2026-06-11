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
    .select(
      "id, project_id, status, error_message, render_r2_key, " +
        "timeline_revision, created_at",
    )
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

// ── DELETE /api/stories/[id] ───────────────────────────────────────────────
// Removes a story whose render failed, so dead rows don't pile up in a round.
// Only error-status stories are deletable: generating/rendering rows have a
// worker attached, and done rows are kept as the project's output history.

export async function DELETE(
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
    .select("id, status")
    .eq("id", storyId)
    .maybeSingle();

  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }
  if (story.status !== "error") {
    return NextResponse.json(
      { error: `Cannot delete story with status '${story.status}'` },
      { status: 409 },
    );
  }

  const { error } = await supabase.from("stories").delete().eq("id", storyId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return new NextResponse(null, { status: 204 });
}
