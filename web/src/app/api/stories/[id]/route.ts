import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { deleteObjects } from "@/lib/r2/client";

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

// ── PATCH /api/stories/[id] ────────────────────────────────────────────────
// Rename a cut (set title/description). Lets API-created cuts — which start
// untitled and show as "Untitled cut" in the web list — be labeled after the
// fact. Body: { title?: string | null, description?: string | null }.

export async function PATCH(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: storyId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  let body: { title?: unknown; description?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  const update: Record<string, string | null> = {};
  for (const key of ["title", "description"] as const) {
    if (body[key] === undefined) continue;
    const v = body[key];
    if (v !== null && typeof v !== "string") {
      return NextResponse.json(
        { error: `${key} must be a string or null` },
        { status: 400 },
      );
    }
    update[key] = typeof v === "string" ? v.trim() || null : null;
  }
  if (Object.keys(update).length === 0) {
    return NextResponse.json(
      { error: "nothing to update — provide title and/or description" },
      { status: 400 },
    );
  }

  // RLS enforces ownership via the project.
  const { data, error } = await supabase
    .from("stories")
    .update(update)
    .eq("id", storyId)
    .select("id, title, description")
    .maybeSingle();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (!data) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }
  return NextResponse.json(data);
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
    .select("id, status, render_r2_key")
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

  // Purge any rendered output before the row so deleting never orphans storage.
  await deleteObjects([story.render_r2_key]);

  const { error } = await supabase.from("stories").delete().eq("id", storyId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return new NextResponse(null, { status: 204 });
}
