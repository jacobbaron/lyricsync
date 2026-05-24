import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Verify clip exists and belongs to user (RLS enforces ownership via project)
  const { data: clip } = await supabase
    .from("clips")
    .select("id, status, project_id")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  // Only trigger if the clip has completed uploading
  if (clip.status !== "uploading_complete") {
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

  // Also transition project to transcribing if it's still at uploading
  await supabase
    .from("projects")
    .update({ status: "transcribing" })
    .eq("id", clip.project_id)
    .eq("status", "uploading");

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
