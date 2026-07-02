import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { requireClipWithVideo } from "@/lib/api/clips";
import { createClipSignal } from "@/lib/api/signals";
import { deferModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/motion ─────────────────────────────────────────────
// PERCEPTION T2: kicks off a camera-motion / shot-dynamics run for a clip
// (static / pan / tilt / zoom / handheld / whip + in-camera shot boundaries).
// Creates a clip_signals row (kind=camera_motion) up front and fires the Modal
// worker; poll GET /api/clips/[id]/signals (optionally ?kind=camera_motion).
//
// API-key callable (Authorization: Bearer lsk_...).

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const clip = await requireClipWithVideo(auth.supabase, clipId);
  if (clip instanceof NextResponse) return clip;

  const signal = await createClipSignal(auth.supabase, clipId, "camera_motion");
  if (signal instanceof NextResponse) return signal;

  deferModal(
    "motion",
    process.env.MODAL_MOTION_URL ??
      "https://jacobbaron--lyricsync-analyze-motion.modal.run",
    { signal_id: signal.id },
  );

  return NextResponse.json(
    { id: signal.id, kind: "camera_motion", status: "processing" },
    { status: 202 },
  );
}
