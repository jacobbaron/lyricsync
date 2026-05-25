import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/render ─────────────────────────────────────────
// (Re-)triggers the Modal render task for a story. Used by:
//   • P1-12 retry button (when story.status === "error")
//   • Can also be called directly by POST /api/projects/[id]/stories
//
// Resets the story to 'rendering' and fires the Modal render endpoint.

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: storyId } = await context.params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Verify story exists and is accessible (RLS enforces ownership)
  const { data: story } = await supabase
    .from("stories")
    .select("id, project_id, status")
    .eq("id", storyId)
    .maybeSingle();

  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  // Reset to 'rendering' and clear any previous error
  await supabase
    .from("stories")
    .update({ status: "rendering", error_message: null })
    .eq("id", storyId);

  // Also reset the project status if needed
  await supabase
    .from("projects")
    .update({ status: "rendering" })
    .eq("id", story.project_id)
    .in("status", ["transcribed", "done", "error"]);

  // Fire Modal render endpoint (fire-and-forget)
  const renderUrl = process.env.MODAL_RENDER_URL;
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (renderUrl && webhookSecret) {
    fetch(renderUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": webhookSecret,
      },
      body: JSON.stringify({ story_id: storyId }),
    }).catch((err) => console.error("[render] Modal trigger failed:", err));
  } else {
    console.warn("[render] MODAL_RENDER_URL not set — render not triggered");
  }

  return NextResponse.json({ status: "accepted" });
}
