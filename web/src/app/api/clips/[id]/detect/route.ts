import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { requireClipWithVideo } from "@/lib/api/clips";
import { createClipSignal } from "@/lib/api/signals";
import { deferModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/detect ─────────────────────────────────────────────
// PERCEPTION T5: kicks off a closed-set object-detection run (YOLOv8n / COCO)
// for a clip. Creates a clip_signals row (kind=detection) up front and fires
// the Modal worker; poll GET /api/clips/[id]/signals?kind=detection — the
// compact per-class inventory is in `result` and the full per-frame boxes are
// in the presigned `sidecar_url` (detections.json).
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

  const signal = await createClipSignal(auth.supabase, clipId, "detection");
  if (signal instanceof NextResponse) return signal;

  deferModal(
    "detect",
    process.env.MODAL_DETECT_URL ??
      "https://jacobbaron--lyricsync-detect-objects.modal.run",
    { signal_id: signal.id },
  );

  return NextResponse.json(
    { id: signal.id, kind: "detection", status: "processing" },
    { status: 202 },
  );
}
