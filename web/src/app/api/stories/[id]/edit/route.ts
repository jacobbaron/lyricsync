import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { proxyModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/edit ────────────────────────────────────────────
// Applies edit operations to the story's timeline (EDL-01). This route only
// authenticates and checks ownership; the op semantics, validation, and
// revision bookkeeping live in the Modal edit_timeline endpoint so there is a
// single (unit-tested, Python) implementation.
//
// Body:
//   ops               edit op array (may be empty — materializes the timeline
//                     from ranges_json on first call)
//   base_revision     optional optimistic-concurrency check (409 on mismatch)
//   restore_revision  optional — reinstate a prior revision (excludes ops)
//
// Returns Modal's response verbatim:
//   200 { revision, duration_secs, timeline }
//   400 { detail: "<op/validation error written for the LLM caller>" }
//   409 { detail: "revision conflict ..." }
//
// See docs/timeline_editing.md for the op vocabulary and timeline schema.

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

  let body: {
    ops?: unknown;
    base_revision?: unknown;
    restore_revision?: unknown;
  };
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  if (body.ops !== undefined && !Array.isArray(body.ops)) {
    return NextResponse.json(
      { error: "ops must be an array of edit operations" },
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

  // MODAL_EDIT_URL points at the Modal `edit_timeline` endpoint. It lives on the
  // same Modal app as `render_story`, so if the env var is missing we derive it
  // from MODAL_RENDER_URL (…-render-story → …-edit-timeline) instead of 503ing —
  // this keeps timeline edits working even when only MODAL_RENDER_URL is set.
  const editUrl =
    process.env.MODAL_EDIT_URL ||
    process.env.MODAL_RENDER_URL?.replace("render-story", "edit-timeline");
  if (!editUrl) {
    return NextResponse.json(
      { error: "MODAL_EDIT_URL not configured (and could not derive it from MODAL_RENDER_URL)" },
      { status: 503 },
    );
  }

  const { payload, status } = await proxyModal(
    editUrl,
    {
      story_id: storyId,
      ops: body.ops ?? [],
      base_revision: body.base_revision ?? null,
      restore_revision: body.restore_revision ?? null,
    },
    "edit service",
  );
  return NextResponse.json(payload, { status });
}
