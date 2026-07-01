import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/embed ──────────────────────────────────────────────
// PERCEPTION T4: kicks off a clip-embedding run — CLIP-encode ~1 fps frames
// into pgvector (clip_embeddings) for semantic search. Creates a clip_signals
// row (kind=embedding) up front to track status and fires the Modal worker;
// poll GET /api/clips/[id]/signals?kind=embedding. Search the results with
// GET /api/projects/[id]/search?q=...
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
    .insert({ clip_id: clipId, kind: "embedding", status: "processing" })
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
  const embedUrl =
    process.env.MODAL_EMBED_URL ??
    "https://jacobbaron--lyricsync-embed-clip.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.warn("[embed] MODAL_WEBHOOK_SECRET not set — embedding not triggered");
  } else {
    const signalId = signal.id;
    after(async () => {
      try {
        const res = await fetch(embedUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-webhook-secret": webhookSecret,
          },
          body: JSON.stringify({ signal_id: signalId }),
        });
        const text = await res.text().catch(() => "");
        console.log(`[embed] Modal responded ${res.status}: ${text}`);
        if (!res.ok) {
          console.error(`[embed] Modal error for ${signalId}: ${text}`);
        }
      } catch (err) {
        console.error("[embed] Modal trigger failed:", err);
      }
    });
  }

  return NextResponse.json(
    { id: signal.id, kind: "embedding", status: "processing" },
    { status: 202 },
  );
}
