import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { deleteObjects } from "@/lib/r2/client";

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

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

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

  // Reset to 'rendering', clear any previous error, optionally update ranges.
  // Setting ranges discards any edited timeline (EDL-01): the timeline is
  // re-materialized from the new ranges on the next edit or render.
  const storyUpdate: Record<string, unknown> = {
    status: "rendering",
    error_message: null,
  };
  if (newRanges) {
    storyUpdate.ranges_json = newRanges;
    storyUpdate.timeline_json = null;
  }

  await supabase.from("stories").update(storyUpdate).eq("id", storyId);

  // The project status is deliberately left alone: render progress lives on the
  // story row, and the render worker never moves the project back, so flipping
  // it to 'rendering' stranded the project there — hiding the upload area
  // (ADD_VIDEOS_STATUSES) for good. /music and /lipsync already render this way.

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

// ── DELETE /api/stories/[id]/render ───────────────────────────────────────
// Frees storage by deleting a cut's rendered output (the MP4 in R2) while
// keeping the story row, its ranges, and its timeline. The cut becomes
// re-renderable: POST to this route (or the StoryCard render button)
// regenerates the output from the persisted edit.
//
// Only 'done' stories have an output to delete; anything else is a 409.

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
  if (story.status !== "done" || !story.render_r2_key) {
    return NextResponse.json(
      { error: "No rendered output to delete for this cut" },
      { status: 409 },
    );
  }

  await deleteObjects([story.render_r2_key]);

  // Drop the output and mark the cut re-renderable; the edit is preserved.
  const { error } = await supabase
    .from("stories")
    .update({ render_r2_key: null, status: "ready" })
    .eq("id", storyId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ status: "deleted" });
}
