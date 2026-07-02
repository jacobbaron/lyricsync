import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { requireClipWithVideo } from "@/lib/api/clips";
import { proxyModal } from "@/lib/modal/trigger";

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

  const clip = await requireClipWithVideo(auth.supabase, clipId);
  if (clip instanceof NextResponse) return clip;

  // analyze_clip_audio lives on the same Modal app as render_story, so derive
  // its URL from MODAL_RENDER_URL (override with MODAL_ANALYZE_AUDIO_URL).
  const analyzeUrl =
    process.env.MODAL_ANALYZE_AUDIO_URL ||
    process.env.MODAL_RENDER_URL?.replace("render-story", "analyze-clip-audio");
  if (!analyzeUrl) {
    return NextResponse.json(
      {
        error:
          "audio analysis is not configured (set MODAL_ANALYZE_AUDIO_URL, or " +
          "MODAL_RENDER_URL to derive it from)",
      },
      { status: 503 },
    );
  }

  const { payload, status } = await proxyModal(
    analyzeUrl,
    { clip_id: clipId },
    "audio analysis service",
  );
  return NextResponse.json(payload, { status });
}
