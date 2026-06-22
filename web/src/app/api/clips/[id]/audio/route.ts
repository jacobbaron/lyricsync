import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { getObjectText, presignDownload } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── GET /api/clips/[id]/audio ───────────────────────────────────────────────
// Returns a clip's audio-analysis document (waveform peaks, VAD curve +
// intervals, words) plus a short-lived presigned URL for the audio proxy, for
// the clip visualization page. 409 until the analysis has been built (POST
// /api/clips/[id]/audio-analysis to kick it off).

const AUDIO_TTL = 3600;

export async function GET(
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
    .select("id, project_id, filename, status")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  const analysisKey = `projects/${clip.project_id}/clips/${clipId}/audio_analysis.json`;
  let analysis: { audio_key?: string };
  try {
    analysis = JSON.parse(await getObjectText(analysisKey));
  } catch {
    return NextResponse.json(
      { error: "Audio analysis not ready", status: clip.status },
      { status: 409 },
    );
  }

  const audioKey =
    analysis.audio_key ||
    `projects/${clip.project_id}/clips/${clipId}/audio.m4a`;
  const audioUrl = await presignDownload(audioKey, AUDIO_TTL);

  return NextResponse.json({
    clip: { id: clip.id, filename: clip.filename },
    audio_url: audioUrl,
    analysis,
  });
}
