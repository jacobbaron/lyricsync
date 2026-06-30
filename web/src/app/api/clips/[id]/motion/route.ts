import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

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
  const { supabase } = auth;

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

  const { data: signal, error } = await supabase
    .from("clip_signals")
    .insert({ clip_id: clipId, kind: "camera_motion", status: "processing" })
    .select("id")
    .single();

  if (error || !signal) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create signal" },
      { status: 500 },
    );
  }

  // Same after()-deferred-fetch pattern as /quality: the 202 returns
  // immediately while Vercel keeps the function alive to finish the call to a
  // (likely cold) Modal container.
  const motionUrl =
    process.env.MODAL_MOTION_URL ??
    "https://jacobbaron--lyricsync-analyze-motion.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.warn("[motion] MODAL_WEBHOOK_SECRET not set — analysis not triggered");
  } else {
    const signalId = signal.id;
    after(async () => {
      try {
        const res = await fetch(motionUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-webhook-secret": webhookSecret,
          },
          body: JSON.stringify({ signal_id: signalId }),
        });
        const text = await res.text().catch(() => "");
        console.log(`[motion] Modal responded ${res.status}: ${text}`);
        if (!res.ok) {
          console.error(`[motion] Modal error for ${signalId}: ${text}`);
        }
      } catch (err) {
        console.error("[motion] Modal trigger failed:", err);
      }
    });
  }

  return NextResponse.json(
    { id: signal.id, kind: "camera_motion", status: "processing" },
    { status: 202 },
  );
}
