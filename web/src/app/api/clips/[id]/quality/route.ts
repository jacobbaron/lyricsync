import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/quality ────────────────────────────────────────────
// PERCEPTION T1: kicks off a technical-quality QC run for a clip (sharpness /
// exposure / shake / black-frozen). Creates a clip_signals row (kind=quality)
// up front and fires the Modal worker; poll GET /api/clips/[id]/signals.
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
    .insert({ clip_id: clipId, kind: "quality", status: "processing" })
    .select("id")
    .single();

  if (error || !signal) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create signal" },
      { status: 500 },
    );
  }

  // Same after()-deferred-fetch pattern as /analyze: the 202 returns
  // immediately while Vercel keeps the function alive to finish the call to a
  // (likely cold) Modal container.
  const qualityUrl =
    process.env.MODAL_QUALITY_URL ??
    "https://jacobbaron--lyricsync-analyze-quality.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.warn("[quality] MODAL_WEBHOOK_SECRET not set — analysis not triggered");
  } else {
    const signalId = signal.id;
    after(async () => {
      try {
        const res = await fetch(qualityUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-webhook-secret": webhookSecret,
          },
          body: JSON.stringify({ signal_id: signalId }),
        });
        const text = await res.text().catch(() => "");
        console.log(`[quality] Modal responded ${res.status}: ${text}`);
        if (!res.ok) {
          console.error(`[quality] Modal error for ${signalId}: ${text}`);
        }
      } catch (err) {
        console.error("[quality] Modal trigger failed:", err);
      }
    });
  }

  return NextResponse.json(
    { id: signal.id, kind: "quality", status: "processing" },
    { status: 202 },
  );
}
