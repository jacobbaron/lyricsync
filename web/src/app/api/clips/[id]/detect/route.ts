import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/detect ─────────────────────────────────────────────
// PERCEPTION T5/T6: kicks off an object-detection run for a clip. Creates a
// clip_signals row (kind=detection) up front and fires the Modal worker; poll
// GET /api/clips/[id]/signals?kind=detection — the compact per-class inventory
// is in `result` and the full per-frame boxes are in the presigned
// `sidecar_url` (detections.json).
//
// Body (all optional):
//   mode    — "closed" (default) → YOLOv8n / COCO 80 classes
//             "open"             → YOLO-World open-vocabulary
//   labels  — string[]; open-vocab text prompts (e.g. ["insulation",
//             "tape measure"]). Ignored for closed mode. When omitted in open
//             mode the worker seeds a default list from the clip's
//             visual_description + a DIY glossary.
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
  const { supabase } = auth;

  const body = await request.json().catch(() => ({}));
  const mode = body?.mode === "open" ? "open" : "closed";
  const rawLabels = Array.isArray(body?.labels) ? body.labels : [];
  const labels = rawLabels
    .filter((l: unknown): l is string => typeof l === "string")
    .map((l: string) => l.trim())
    .filter((l: string) => l.length > 0);

  const { data: clip } = await supabase
    .from("clips")
    .select("id, r2_key")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }
  if (!clip.r2_key) {
    return NextResponse.json(
      { error: "Clip has no uploaded video yet" },
      { status: 409 },
    );
  }

  const params =
    mode === "open" ? { mode, labels } : { mode };

  const { data: signal, error } = await supabase
    .from("clip_signals")
    .insert({ clip_id: clipId, kind: "detection", status: "processing", params })
    .select("id")
    .single();

  if (error || !signal) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create signal" },
      { status: 500 },
    );
  }

  // Same after()-deferred-fetch pattern as /quality and /motion: the 202
  // returns immediately while Vercel keeps the function alive to finish the
  // call to a (likely cold) Modal container.
  const detectUrl =
    process.env.MODAL_DETECT_URL ??
    "https://jacobbaron--lyricsync-detect-objects.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.warn("[detect] MODAL_WEBHOOK_SECRET not set — detection not triggered");
  } else {
    const signalId = signal.id;
    after(async () => {
      try {
        const res = await fetch(detectUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-webhook-secret": webhookSecret,
          },
          body: JSON.stringify({ signal_id: signalId }),
        });
        const text = await res.text().catch(() => "");
        console.log(`[detect] Modal responded ${res.status}: ${text}`);
        if (!res.ok) {
          console.error(`[detect] Modal error for ${signalId}: ${text}`);
        }
      } catch (err) {
        console.error("[detect] Modal trigger failed:", err);
      }
    });
  }

  return NextResponse.json(
    { id: signal.id, kind: "detection", status: "processing", mode, params },
    { status: 202 },
  );
}
