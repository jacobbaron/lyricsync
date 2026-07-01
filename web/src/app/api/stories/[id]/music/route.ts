import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/music ────────────────────────────────────────────
// Set (or clear) the external music track under a story's footage, then
// re-render. Stored in stories.music_json; the render worker injects it as
// timeline.music (see modal/timeline.py). Use this to place/nudge a song
// manually; POST /api/stories/[id]/align-music computes song_start for you.
//
// Body: { song_id, song_start, gain_db?, scratch_gain_db? } to set,
//        or { song_id: null } to clear the track.

const SetBody = z.object({
  song_id: z.string().uuid(),
  song_start: z.number().min(0).max(36000),
  gain_db: z.number().min(-40).max(20).optional(),
  // null (or omitted) → replace clip audio; a number → duck clip audio to that
  // dB and mix the song over it.
  scratch_gain_db: z.number().min(-60).max(0).nullable().optional(),
});
const ClearBody = z.object({ song_id: z.null() });
const Body = z.union([SetBody, ClearBody]);

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: storyId } = await context.params;

  const parsed = Body.safeParse(await request.json().catch(() => null));
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid request body", issues: parsed.error.issues },
      { status: 400 },
    );
  }

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data: story } = await supabase
    .from("stories")
    .select("id, project_id, status")
    .eq("id", storyId)
    .maybeSingle();
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  let musicJson: Record<string, unknown> | null = null;
  if (parsed.data.song_id !== null) {
    // The song must exist, belong to the same project, and be uploaded.
    const { data: song } = await supabase
      .from("songs")
      .select("id, project_id, status")
      .eq("id", parsed.data.song_id)
      .maybeSingle();
    if (!song || song.project_id !== story.project_id) {
      return NextResponse.json(
        { error: "Song not found in this project" },
        { status: 404 },
      );
    }
    if (song.status !== "ready") {
      return NextResponse.json(
        { error: `Song not ready (status: ${song.status})` },
        { status: 409 },
      );
    }
    musicJson = {
      song_id: parsed.data.song_id,
      song_start: parsed.data.song_start,
      gain_db: parsed.data.gain_db ?? 0,
      scratch_gain_db: parsed.data.scratch_gain_db ?? null,
    };
  }

  await supabase
    .from("stories")
    .update({ music_json: musicJson, status: "rendering", error_message: null })
    .eq("id", storyId);

  // Fire Modal render (fire-and-forget), same pattern as the render route.
  const renderUrl = process.env.MODAL_RENDER_URL;
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (renderUrl && webhookSecret) {
    fetch(renderUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": webhookSecret,
      },
      body: JSON.stringify({ story_id: storyId }),
    }).catch((err) => console.error("[music] Modal render trigger failed:", err));
  } else {
    console.warn("[music] MODAL_RENDER_URL not set — render not triggered");
  }

  return NextResponse.json({ status: "accepted", music: musicJson });
}
