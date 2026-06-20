import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  // API key (Bearer lsk_...) or browser session — agents/ops drive this too,
  // e.g. to re-run alignment (and speaker diarization) on a finished project.
  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  // Verify project exists and belongs to user (RLS enforces ownership)
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Fetch all clips. The align worker re-reads each clip's raw transcript, so a
  // clip is eligible once it has finished raw transcription — either freshly
  // (transcribed_raw) or on a re-align of an already-processed project
  // (aligned). Anything still uploading/transcribing blocks.
  const { data: clips } = await supabase
    .from("clips")
    .select("id, status")
    .eq("project_id", projectId);

  if (!clips || clips.length === 0) {
    return NextResponse.json({ error: "No clips found" }, { status: 400 });
  }

  const READY = new Set(["transcribed_raw", "aligned"]);
  const notReady = clips.filter((c) => !READY.has(c.status));
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
