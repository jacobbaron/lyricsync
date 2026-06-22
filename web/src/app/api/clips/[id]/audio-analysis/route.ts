import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/audio-analysis ─────────────────────────────────────
// Triggers the Modal worker that builds a clip's audio proxy + waveform + VAD
// speech-probability curve (stored in R2; read back via GET /api/clips/[id]/audio).
// Auth + ownership only; the heavy lifting is the Modal analyze_clip_audio job.

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

  // RLS enforces ownership via the projects join.
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
      { error: "Clip has no uploaded source yet" },
      { status: 409 },
    );
  }

  // analyze_clip_audio lives on the same Modal app as render_story, so derive
  // its URL from MODAL_RENDER_URL (override with MODAL_ANALYZE_AUDIO_URL).
  const analyzeUrl =
    process.env.MODAL_ANALYZE_AUDIO_URL ||
    process.env.MODAL_RENDER_URL?.replace("render-story", "analyze-clip-audio");
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!analyzeUrl || !webhookSecret) {
    return NextResponse.json(
      {
        error:
          "audio analysis is not configured (set MODAL_ANALYZE_AUDIO_URL, or " +
          "MODAL_RENDER_URL to derive it from)",
      },
      { status: 503 },
    );
  }

  const upstream = await fetch(analyzeUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-webhook-secret": webhookSecret,
    },
    body: JSON.stringify({ clip_id: clipId }),
  });

  const payload = await upstream.json().catch(() => ({
    error: "audio analysis service returned a non-JSON response",
  }));
  return NextResponse.json(payload, { status: upstream.status });
}
