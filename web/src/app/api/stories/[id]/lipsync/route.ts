import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";
import {
  type Bed,
  bedStartFor,
  coveringAlignment,
  resolveClipId,
  triggerRender,
  videoItems,
} from "@/lib/songs/bed";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/lipsync ──────────────────────────────────────────
// Lip-sync one clip (the "hero" clip) to the story's music bed: anchors the bed
// so that, at the clip's position in the cut, the song lines up with the clip's
// footage. The bed keeps playing continuously — you can cut to the hero clip and
// away from it. Requires a bed (POST .../music) and a ready alignment covering
// the clip's footage: uploading a song syncs every clip automatically
// (POST /api/songs/[id]/sync), or align one window with
// POST /api/clips/[id]/align-song.
//
// Body: { item_id } — which clip to anchor to. For a ranges-only story the ids
// are "v1", "v2", … in cut order (matching the render's materialization).
//
// v1 anchors the bed to a single hero clip. Repositioning multiple independent
// lip-sync moments is a documented follow-up.

const Body = z.object({ item_id: z.string().min(1) });

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
    .select("id, project_id, ranges_json, timeline_json, music_json, render_epoch")
    .eq("id", storyId)
    .maybeSingle();
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  const bed = story.music_json as Bed | null;
  if (!bed?.song_id) {
    return NextResponse.json(
      { error: "Set a music bed first (POST /api/stories/[id]/music)" },
      { status: 409 },
    );
  }

  const target = videoItems(story).find((it) => it.id === parsed.data.item_id);
  if (!target) {
    return NextResponse.json(
      { error: `item ${parsed.data.item_id} not found in the cut` },
      { status: 400 },
    );
  }
  if (!target.isClip) {
    return NextResponse.json(
      { error: "target item is not a clip" },
      { status: 400 },
    );
  }

  const clipId = await resolveClipId(supabase, story.project_id, target);
  if (!clipId) {
    return NextResponse.json(
      { error: "Could not resolve the clip for this item" },
      { status: 400 },
    );
  }

  const align = await coveringAlignment(supabase, clipId, bed.song_id, target);
  if (!align) {
    return NextResponse.json(
      {
        error:
          "No ready alignment covers this clip's footage — sync the song (POST /api/songs/[id]/sync) or run POST /api/clips/[id]/align-song for this window first",
      },
      { status: 409 },
    );
  }

  const newBed = {
    ...bed,
    song_start: Number(bedStartFor(target, align).toFixed(3)),
  };

  await supabase
    .from("stories")
    .update({
      music_json: newBed,
      status: "rendering",
      error_message: null,
      render_epoch: (story.render_epoch ?? 0) + 1,
    })
    .eq("id", storyId);

  triggerRender(storyId, "lipsync");

  return NextResponse.json({
    status: "accepted",
    item_id: parsed.data.item_id,
    song_start: newBed.song_start,
  });
}
