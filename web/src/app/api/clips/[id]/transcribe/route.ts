import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  // Accept either an API key (Authorization: Bearer lsk_...) or a browser
  // session, like the analyze/render routes — so transcription is scriptable.
  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  // Verify clip exists and belongs to user (RLS enforces ownership via project)
  const { data: clip } = await supabase
    .from("clips")
    .select("id, status, project_id")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  // Trigger for freshly uploaded clips, and allow re-triggering after a
  // failed transcription (the worker left the clip in 'error').
  if (clip.status !== "uploading_complete" && clip.status !== "error") {
    return NextResponse.json(
      { error: `Cannot transcribe clip with status '${clip.status}'` },
      { status: 409 },
    );
  }

  // Update clip status to transcribing immediately so the UI reflects it
  await supabase
    .from("clips")
    .update({ status: "transcribing" })
    .eq("id", clipId);

  // Transition the project to transcribing from any settled state. This covers
  // both the initial upload ('uploading') and adding clips to a project whose
  // transcription already finished ('transcribed' and downstream states), which
  // re-runs the merge over the full clip set once the new clip is transcribed.
  await supabase
    .from("projects")
    .update({ status: "transcribing", error_message: null })
    .eq("id", clip.project_id)
    .in("status", ["uploading", "transcribed", "stories_ready", "done", "error"]);

  // Invoke Modal asynchronously — fire and don't await the response body.
  // Modal's endpoint returns {"status":"accepted"} immediately and does the
  // work in a spawned background container.
  const modalUrl = process.env.MODAL_TRANSCRIBE_URL;
  const modalSecret = process.env.MODAL_WEBHOOK_SECRET;

  if (!modalUrl || !modalSecret) {
    // Dev/test: env not configured, just leave status as transcribing.
    console.warn("MODAL_TRANSCRIBE_URL or MODAL_WEBHOOK_SECRET not set — skipping Modal call");
    return NextResponse.json({ status: "accepted", modal: false }, { status: 202 });
  }

  try {
    const res = await fetch(modalUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": modalSecret,
      },
      body: JSON.stringify({ clip_id: clipId }),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      console.error(`Modal returned ${res.status}: ${text}`);
      // Don't fail the request — clip is already marked transcribing,
      // an operator can retry via the DB or a future retry button.
    }
  } catch (err) {
    console.error("Failed to reach Modal:", err);
  }

  return NextResponse.json({ status: "accepted" }, { status: 202 });
}
