import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";
import { triggerModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/music ────────────────────────────────────────────
// Set (or clear) the music BED under a story — a finished song that plays under
// the whole cut while the video cuts over it — then re-render. Stored in
// stories.music_json and injected as timeline.music by the render worker
// (modal/timeline.py). Lip-sync a specific clip to this bed with
// POST /api/stories/[id]/lipsync.
//
// Body: { song_id, song_start, gain_db?, fade_in_s?, fade_out_s? } to set the
// bed (song_start = song time aligned to output t=0), or { song_id: null } to
// remove it.

const SetBody = z.object({
  song_id: z.string().uuid(),
  song_start: z.number().min(0).max(36000),
  gain_db: z.number().min(-40).max(20).optional(),
  fade_in_s: z.number().min(0).max(30).optional(),
  fade_out_s: z.number().min(0).max(30).optional(),
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
    .select("id, project_id, render_epoch")
    .eq("id", storyId)
    .maybeSingle();
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  let bed: Record<string, unknown> | null = null;
  if (parsed.data.song_id !== null) {
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
    bed = {
      song_id: parsed.data.song_id,
      song_start: parsed.data.song_start,
      gain_db: parsed.data.gain_db ?? 0,
      fade_in_s: parsed.data.fade_in_s ?? 0,
      fade_out_s: parsed.data.fade_out_s ?? 0,
    };
  }

  await supabase
    .from("stories")
    .update({
      music_json: bed,
      status: "rendering",
      error_message: null,
      render_epoch: (story.render_epoch ?? 0) + 1,
    })
    .eq("id", storyId);

  triggerModal("music", process.env.MODAL_RENDER_URL, { story_id: storyId });

  return NextResponse.json({ status: "accepted", music: bed });
}
