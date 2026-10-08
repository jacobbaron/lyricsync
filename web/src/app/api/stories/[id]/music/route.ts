import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";
import {
  bedStartFor,
  coveringAlignment,
  resolveClipId,
  triggerRender,
  videoItems,
} from "@/lib/songs/bed";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/music ────────────────────────────────────────────
// Set (or clear) the music BED under a story — a finished song that plays under
// the whole cut while the video cuts over it — then re-render. Stored in
// stories.music_json and injected as timeline.music by the render worker
// (modal/timeline.py). Lip-sync a specific clip to this bed with
// POST /api/stories/[id]/lipsync.
//
// Body: { song_id, song_start?, gain_db?, fade_in_s?, fade_out_s? } to set the
// bed (song_start = song time aligned to output t=0), or { song_id: null } to
// remove it. Omit song_start to lip-sync the bed to the cut's first clip, using
// that clip's sync to the song (made automatically on song upload); 409 if the
// first clip has no ready alignment covering its footage.

const SetBody = z.object({
  song_id: z.string().uuid(),
  song_start: z.number().min(0).max(36000).optional(),
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
    .select("id, project_id, ranges_json, timeline_json, render_epoch")
    .eq("id", storyId)
    .maybeSingle();
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  let bed: Record<string, unknown> | null = null;
  let songStartSource: "given" | "first_clip" | null = null;
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
    let songStart = parsed.data.song_start;
    songStartSource = "given";
    if (songStart === undefined) {
      const first = videoItems(story).find((it) => it.isClip);
      const clipId = first
        ? await resolveClipId(supabase, story.project_id, first)
        : undefined;
      const align =
        first && clipId
          ? await coveringAlignment(supabase, clipId, parsed.data.song_id, first)
          : undefined;
      if (!first || !align) {
        return NextResponse.json(
          {
            error:
              "No song_start given and the cut's first clip has no ready sync to this song — pass song_start, or sync the song (POST /api/songs/[id]/sync) and retry",
          },
          { status: 409 },
        );
      }
      songStart = Number(bedStartFor(first, align).toFixed(3));
      songStartSource = "first_clip";
    }
    bed = {
      song_id: parsed.data.song_id,
      song_start: songStart,
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

  triggerRender(storyId, "music");

  return NextResponse.json({
    status: "accepted",
    music: bed,
    song_start_source: songStartSource,
  });
}
