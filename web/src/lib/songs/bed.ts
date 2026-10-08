import type { SupabaseClient } from "@supabase/supabase-js";

// Shared music-bed math for /api/stories/[id]/{music,lipsync,lyric-captions}.
//
// A bed is stories.music_json = {song_id, song_start, …}: the song plays under
// the whole cut, and song_start is the song time at output t=0. So
//   output_t = song_t - song_start
// A clip_alignments row maps a clip's footage to song time:
//   song_t = footage_t - footage_start + song_start(alignment)

// Padding the render bakes around each range (modal/timeline.py RANGE_PAD_S), so
// the offsets here match what actually renders.
export const RANGE_PAD = 0.08;

export type Bed = {
  song_id?: string;
  song_start?: number;
  gain_db?: number;
  fade_in_s?: number;
  fade_out_s?: number;
};

export type VideoItem = {
  id: string;
  isClip: boolean;
  clipId?: string;
  source?: string;
  srcStart: number; // rendered footage in-point
  srcEnd: number;
  outputOffset: number; // seconds into the output where this item starts
  duration: number; // seconds of output this item occupies
};

type StoryEdit = { timeline_json: unknown; ranges_json: unknown };

// The story's video items in cut order, from timeline_json or, if absent,
// ranges_json — reproducing the render's per-range padding so offsets line up.
export function videoItems(story: StoryEdit): VideoItem[] {
  const tl = story.timeline_json as
    | { tracks?: { type?: string; items?: Record<string, unknown>[] }[] }
    | null;
  const out: VideoItem[] = [];
  let offset = 0;

  if (tl?.tracks) {
    const items = tl.tracks.find((t) => t.type === "video")?.items ?? [];
    for (const it of items) {
      const isClip = it.kind === "clip";
      const dur = isClip
        ? (Number(it.src_end) - Number(it.src_start)) / (Number(it.speed) || 1)
        : Number(it.duration ?? 0);
      out.push({
        id: String(it.id),
        isClip,
        clipId: (it.clip_id as string) || undefined,
        source: (it.source as string) || undefined,
        srcStart: Number(it.src_start ?? 0),
        srcEnd: Number(it.src_end ?? 0),
        outputOffset: offset,
        duration: dur,
      });
      offset += dur;
    }
    return out;
  }

  // Ranges-only: ids are v1..vN in order (as timeline_from_ranges assigns them).
  const ranges = (story.ranges_json as Record<string, unknown>[] | null) ?? [];
  ranges.forEach((r, i) => {
    const start = Number(r.start);
    const end = Number(r.end);
    const isBlank = r.source === "blank";
    const srcStart = isBlank ? start : Math.max(0, start - RANGE_PAD);
    const srcEnd = isBlank ? end : end + RANGE_PAD;
    const dur = srcEnd - srcStart;
    out.push({
      id: `v${i + 1}`,
      isClip: !isBlank,
      source: r.source as string,
      srcStart,
      srcEnd,
      outputOffset: offset,
      duration: dur,
    });
    offset += dur;
  });
  return out;
}

// Total output duration of the cut, in seconds.
export function cutDuration(story: StoryEdit): number {
  return videoItems(story).reduce((acc, it) => acc + it.duration, 0);
}

// Resolve an item's clip id: timeline items carry clip_id; ranges carry the
// clip filename as `source`, looked up within the story's project.
export async function resolveClipId(
  supabase: SupabaseClient,
  projectId: string,
  item: VideoItem,
): Promise<string | undefined> {
  if (item.clipId) return item.clipId;
  if (!item.source) return undefined;
  const { data: clip } = await supabase
    .from("clips")
    .select("id")
    .eq("project_id", projectId)
    .eq("filename", item.source)
    .maybeSingle();
  return clip?.id;
}

// A ready alignment of this clip to this song whose footage window covers the
// item's rendered footage (with a PAD tolerance, so a window aligned to the
// exact in/out point still qualifies); prefers the most recent.
export async function coveringAlignment(
  supabase: SupabaseClient,
  clipId: string,
  songId: string,
  item: VideoItem,
): Promise<{ footage_start: number; song_start: number } | undefined> {
  const { data: aligns } = await supabase
    .from("clip_alignments")
    .select("footage_start, footage_end, song_start, status")
    .eq("clip_id", clipId)
    .eq("song_id", songId)
    .eq("status", "ready")
    .order("created_at", { ascending: false });
  const a = (aligns ?? []).find(
    (a) =>
      a.song_start != null &&
      Number(a.footage_start) <= item.srcStart + RANGE_PAD + 1e-3 &&
      Number(a.footage_end) >= item.srcEnd - RANGE_PAD - 1e-3,
  );
  return a
    ? { footage_start: Number(a.footage_start), song_start: Number(a.song_start) }
    : undefined;
}

// The bed song_start that lip-syncs `item` to the song: at the item's output
// position the bed must be at the footage's song time.
//   bed.song_start = (srcStart + offset) - outputOffset
// where offset = alignment.song_start - alignment.footage_start.
export function bedStartFor(
  item: VideoItem,
  align: { footage_start: number; song_start: number },
): number {
  const offset = align.song_start - align.footage_start;
  return Math.max(0, item.srcStart + offset - item.outputOffset);
}

// Fire the Modal render for a story (fire-and-forget; the caller has already
// flipped the row to 'rendering').
export function triggerRender(storyId: string, tag: string): void {
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
    }).catch((err) => console.error(`[${tag}] Modal render trigger failed:`, err));
  } else {
    console.warn(`[${tag}] MODAL_RENDER_URL not set — render not triggered`);
  }
}
