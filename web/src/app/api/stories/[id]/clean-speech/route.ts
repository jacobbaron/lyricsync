import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { proxyModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/clean-speech ─────────────────────────────────────
// Dry-runs a `clean_speech` cleanup on one timeline clip item: returns the
// filler/silence the planner would remove, the time saved, and the resulting
// timeline — WITHOUT persisting anything. To actually apply it, send
// `{op: "clean_speech", id, params}` to /api/stories/[id]/edit.
//
// This route only authenticates and checks ownership; the planning lives in
// the Modal preview_clean_speech endpoint so there is a single (unit-tested,
// Python) implementation. See docs/timeline_editing.md.
//
// Body:
//   id      (required) the video item id to clean
//   params  optional cleanup params (max_gap, collapse_to, remove_fillers,
//           filler_lexicon, pad_start/end, protect_gap_over, trim_lead/tail,
//           min_score/low_score_pad, min_removed, join)
//
// Returns Modal's response verbatim:
//   200 { item_id, revision, plan, duration_secs, timeline }
//   400 { detail: "<planner/validation error written for the LLM caller>" }

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

  let body: { id?: unknown; params?: unknown };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (typeof body.id !== "string" || !body.id) {
    return NextResponse.json(
      { error: "id (the video item to clean) is required" },
      { status: 400 },
    );
  }
  if (
    body.params !== undefined &&
    (typeof body.params !== "object" || body.params === null || Array.isArray(body.params))
  ) {
    return NextResponse.json(
      { error: "params must be an object" },
      { status: 400 },
    );
  }

  // Verify story exists and is accessible (RLS enforces ownership)
  const { data: story } = await supabase
    .from("stories")
    .select("id")
    .eq("id", storyId)
    .maybeSingle();

  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  // The preview endpoint lives on the same Modal app as edit_timeline /
  // render_story, so derive its URL from whichever is configured rather than
  // requiring a new env var (an explicit MODAL_PREVIEW_CLEAN_URL still wins).
  const previewUrl =
    process.env.MODAL_PREVIEW_CLEAN_URL ||
    process.env.MODAL_EDIT_URL?.replace("edit-timeline", "preview-clean-speech") ||
    process.env.MODAL_RENDER_URL?.replace("render-story", "preview-clean-speech");
  if (!previewUrl) {
    return NextResponse.json(
      {
        error:
          "clean-speech preview is not configured (set MODAL_PREVIEW_CLEAN_URL, " +
          "or MODAL_EDIT_URL / MODAL_RENDER_URL to derive it from)",
      },
      { status: 503 },
    );
  }

  const { payload, status } = await proxyModal(
    previewUrl,
    {
      story_id: storyId,
      id: body.id,
      params: body.params ?? {},
    },
    "clean-speech preview service",
  );
  return NextResponse.json(payload, { status });
}
