import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";

export async function POST(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Verify project exists and belongs to user (RLS enforces ownership)
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Fetch all clips and verify every one has finished raw transcription
  const { data: clips } = await supabase
    .from("clips")
    .select("id, status")
    .eq("project_id", projectId);

  if (!clips || clips.length === 0) {
    return NextResponse.json({ error: "No clips found" }, { status: 400 });
  }

  const notReady = clips.filter((c) => c.status !== "transcribed_raw");
  if (notReady.length > 0) {
    return NextResponse.json(
      {
        error: `${notReady.length} clip(s) not yet transcribed`,
        statuses: clips.map((c) => ({ id: c.id, status: c.status })),
      },
      { status: 409 },
    );
  }

  // Invoke Modal asynchronously — fire and don't await the response body.
  const modalUrl = process.env.MODAL_ALIGN_URL;
  const modalSecret = process.env.MODAL_WEBHOOK_SECRET;

  if (!modalUrl || !modalSecret) {
    // Dev/test: env not configured — log and return accepted so the client
    // isn't stuck waiting.
    console.warn(
      "MODAL_ALIGN_URL or MODAL_WEBHOOK_SECRET not set — skipping Modal call",
    );
    return NextResponse.json(
      { status: "accepted", modal: false },
      { status: 202 },
    );
  }

  try {
    const res = await fetch(modalUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": modalSecret,
      },
      body: JSON.stringify({ project_id: projectId }),
    });

    if (!res.ok) {
      const text = await res.text().catch(() => "");
      console.error(`Modal align returned ${res.status}: ${text}`);
      // Don't fail the request — an operator can retry via the DB or UI.
    }
  } catch (err) {
    console.error("Failed to reach Modal align endpoint:", err);
  }

  return NextResponse.json({ status: "accepted" }, { status: 202 });
}
