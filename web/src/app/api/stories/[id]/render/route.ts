import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/render ─────────────────────────────────────────
// (Re-)triggers the Modal render task for a story. Used by:
//   • P1-12 retry button (when story.status === "error")
//   • StoryCard "Render this cut" button (P2-03), which may pass updated ranges
//
// Optional body: { ranges: [{ source, start, end }] }
//   If provided, updates story.ranges_json before triggering render.
//
// Resets the story to 'rendering' and fires the Modal render endpoint.

interface Range {
  source: string;
  start: number;
  end: number;
}

export async function POST(
  request: Request,
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

  // Parse optional ranges from body
  let newRanges: Range[] | undefined;
  try {
    const body = await request.json();
    if (body?.ranges && Array.isArray(body.ranges) && body.ranges.length > 0) {
      newRanges = body.ranges as Range[];
      for (const r of newRanges) {
        if (
          typeof r.source !== "string" ||
          typeof r.start !== "number" ||
          typeof r.end !== "number" ||
          r.end <= r.start
        ) {
          return NextResponse.json(
            { error: "each range must have source, start, end (end > start)" },
            { status: 400 },
          );
        }
      }
    }
  } catch {
    // No body or non-JSON — fine, ranges stay as-is
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

  // Reset to 'rendering', clear any previous error, optionally update ranges
  const storyUpdate: Record<string, unknown> = {
    status: "rendering",
    error_message: null,
  };
  if (newRanges) storyUpdate.ranges_json = newRanges;

  await supabase.from("stories").update(storyUpdate).eq("id", storyId);

  // Also reset the project status if needed
  await supabase
    .from("projects")
    .update({ status: "rendering" })
    .eq("id", story.project_id)
    .in("status", ["transcribed", "stories_ready", "done", "error"]);

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
