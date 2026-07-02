import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";
import { triggerModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/lipsync ──────────────────────────────────────────
// Lip-sync one clip (the "hero" clip) to the story's music bed: anchors the bed
// so that, at the clip's position in the cut, the song lines up with the clip's
// footage. The bed keeps playing continuously — you can cut to the hero clip and
// away from it. Requires a bed (POST .../music) and a ready alignment covering
// the clip's footage (POST /api/clips/[id]/align-song).
//
// Body: { item_id } — which clip to anchor to. For a ranges-only story the ids
// are "v1", "v2", … in cut order (matching the render's materialization).
//
// v1 anchors the bed to a single hero clip. Repositioning multiple independent
// lip-sync moments is a documented follow-up.

const Body = z.object({ item_id: z.string().min(1) });

// Padding the render bakes around each range (modal/timeline.py RANGE_PAD_S), so
// the offsets here match what actually renders.
const PAD = 0.08;

type Target = {
  clipId?: string;
  source?: string;
  srcStart: number; // rendered footage in-point
  srcEnd: number;
  outputOffset: number; // seconds into the output where this clip starts
};

// Resolve a target clip (by item id) from a timeline_json or, if absent, from
// ranges_json — reproducing the render's per-range padding so offsets line up.
function resolveTarget(story: {
  timeline_json: unknown;
  ranges_json: unknown;
}, itemId: string): Target | { error: string } {
  const tl = story.timeline_json as
    | { tracks?: { type?: string; items?: Record<string, unknown>[] }[] }
    | null;
  if (tl?.tracks) {
    const items =
      tl.tracks.find((t) => t.type === "video")?.items ?? [];
    let offset = 0;
    for (const it of items) {
      const dur =
        it.kind === "blank"
          ? Number(it.duration ?? 0)
          : (Number(it.src_end) - Number(it.src_start)) /
            (Number(it.speed) || 1);
      if (it.id === itemId) {
        if (it.kind !== "clip") return { error: "target item is not a clip" };
        return {
          clipId: (it.clip_id as string) || undefined,
          source: (it.source as string) || undefined,
          srcStart: Number(it.src_start),
          srcEnd: Number(it.src_end),
          outputOffset: offset,
        };
      }
      offset += dur;
    }
    return { error: `item ${itemId} not found in timeline` };
  }

  // Ranges-only: ids are v1..vN in order (as timeline_from_ranges assigns them).
  const ranges = (story.ranges_json as Record<string, unknown>[] | null) ?? [];
  const idx = /^v(\d+)$/.exec(itemId);
  let offset = 0;
  for (let i = 0; i < ranges.length; i++) {
    const r = ranges[i];
    const start = Number(r.start);
    const end = Number(r.end);
    const isBlank = r.source === "blank";
    const srcStart = isBlank ? start : Math.max(0, start - PAD);
    const srcEnd = isBlank ? end : end + PAD;
    const dur = srcEnd - srcStart;
    if (idx && Number(idx[1]) === i + 1) {
      if (isBlank) return { error: "target item is not a clip" };
      return {
        source: r.source as string,
        srcStart,
        srcEnd,
        outputOffset: offset,
      };
    }
    offset += dur;
  }
  return { error: `item ${itemId} not found in ranges` };
}

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

  const bed = story.music_json as
    | { song_id?: string; song_start?: number }
    | null;
  if (!bed?.song_id) {
    return NextResponse.json(
      { error: "Set a music bed first (POST /api/stories/[id]/music)" },
      { status: 409 },
    );
  }

  const target = resolveTarget(story, parsed.data.item_id);
  if ("error" in target) {
    return NextResponse.json({ error: target.error }, { status: 400 });
  }

  // Resolve the clip id (for the alignment lookup).
  let clipId = target.clipId;
  if (!clipId && target.source) {
    const { data: clip } = await supabase
      .from("clips")
      .select("id")
      .eq("project_id", story.project_id)
      .eq("filename", target.source)
      .maybeSingle();
    clipId = clip?.id;
  }
  if (!clipId) {
    return NextResponse.json(
      { error: "Could not resolve the clip for this item" },
      { status: 400 },
    );
  }

  // Find a ready alignment for this clip + bed song whose footage window covers
  // the clip's in-point; prefer the most recent.
  const { data: aligns } = await supabase
    .from("clip_alignments")
    .select("footage_start, footage_end, song_start, status")
    .eq("clip_id", clipId)
    .eq("song_id", bed.song_id)
    .eq("status", "ready")
    .order("created_at", { ascending: false });
  // The render bakes ±PAD around each range, so the shown footage runs from
  // srcStart-ish to srcEnd; require the alignment window to cover it, with a PAD
  // tolerance so a window aligned to the exact in/out point still qualifies.
  const align = (aligns ?? []).find(
    (a) =>
      a.song_start != null &&
      Number(a.footage_start) <= target.srcStart + PAD + 1e-3 &&
      Number(a.footage_end) >= target.srcEnd - PAD - 1e-3,
  );
  if (!align) {
    return NextResponse.json(
      {
        error:
          "No ready alignment covers this clip's footage — run POST /api/clips/[id]/align-song for this window first",
      },
      { status: 409 },
    );
  }

  // offset: song_time = footage_time - footage_start + song_start.
  const offset = Number(align.song_start) - Number(align.footage_start);
  // Anchor the bed so bed(song) at the clip's output position == the footage's
  // song time: bed.song_start = (srcStart + offset) - outputOffset.
  const newSongStart = Math.max(
    0,
    target.srcStart + offset - target.outputOffset,
  );

  const newBed = { ...bed, song_start: Number(newSongStart.toFixed(3)) };

  await supabase
    .from("stories")
    .update({
      music_json: newBed,
      status: "rendering",
      error_message: null,
      render_epoch: (story.render_epoch ?? 0) + 1,
    })
    .eq("id", storyId);

  triggerModal("lipsync", process.env.MODAL_RENDER_URL, { story_id: storyId });

  return NextResponse.json({
    status: "accepted",
    item_id: parsed.data.item_id,
    song_start: newBed.song_start,
  });
}
