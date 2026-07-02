import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { triggerModal } from "@/lib/modal/trigger";

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

  // Optional diarization speaker-count constraints, forwarded to pyannote.
  // Body is optional; a malformed/empty body just means "no constraints".
  let body: Record<string, unknown> = {};
  try {
    body = (await request.json()) as Record<string, unknown>;
  } catch {
    body = {};
  }
  const asPosInt = (v: unknown): number | undefined => {
    const n = Math.trunc(Number(v));
    return Number.isFinite(n) && n >= 1 ? n : undefined;
  };
  const speakerArgs: Record<string, number> = {};
  for (const key of ["num_speakers", "min_speakers", "max_speakers"]) {
    const n = asPosInt(body[key]);
    if (n !== undefined) speakerArgs[key] = n;
  }

  triggerModal("align", process.env.MODAL_ALIGN_URL, {
    project_id: projectId,
    ...speakerArgs,
  });

  return NextResponse.json({ status: "accepted" }, { status: 202 });
}
