import type { SupabaseClient } from "@supabase/supabase-js";

// Auto-sync a song to every clip of its project (issue #160): one
// clip_alignments row per clip covering the clip's full duration, each handed
// to the Modal music-align worker. The worker tries the same-take fast path
// (onset xcorr + drift check, modal/song_sync.py) and falls back to chroma-DTW.
//
// Clips with no known duration yet (still transcribing) are skipped; re-run
// POST /api/songs/[id]/sync once they finish.

export type SyncSummary = {
  queued: { clipId: string; alignmentId: string }[];
  skipped: { clipId: string; reason: string }[];
  triggered: boolean;
};

export async function startSongSync(
  supabase: SupabaseClient,
  song: { id: string; project_id: string },
): Promise<SyncSummary> {
  const summary: SyncSummary = { queued: [], skipped: [], triggered: false };

  const { data: clips } = await supabase
    .from("clips")
    .select("id, duration_secs, r2_key")
    .eq("project_id", song.project_id);

  const rows: { clip_id: string; song_id: string; footage_start: number; footage_end: number; status: string }[] = [];
  for (const c of clips ?? []) {
    const dur = Number(c.duration_secs);
    if (!c.r2_key) {
      summary.skipped.push({ clipId: c.id, reason: "no media yet" });
    } else if (!(dur > 0)) {
      summary.skipped.push({ clipId: c.id, reason: "duration unknown (still processing)" });
    } else {
      rows.push({
        clip_id: c.id,
        song_id: song.id,
        footage_start: 0,
        footage_end: dur,
        status: "aligning",
      });
    }
  }
  if (rows.length === 0) return summary;

  const { data: inserted, error } = await supabase
    .from("clip_alignments")
    .insert(rows)
    .select("id, clip_id");
  if (error || !inserted) {
    throw new Error(error?.message ?? "Failed to create clip alignments");
  }
  summary.queued = inserted.map((a) => ({ clipId: a.clip_id, alignmentId: a.id }));

  const modalUrl = process.env.MODAL_MUSIC_ALIGN_URL;
  const modalSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!modalUrl || !modalSecret) {
    console.warn("[song-sync] MODAL_MUSIC_ALIGN_URL not set — not triggered");
    return summary;
  }
  // Await the triggers (the Modal endpoint only enqueues, so this is quick):
  // a fire-and-forget fetch can be dropped when the serverless function
  // returns, and here there are several of them.
  const results = await Promise.allSettled(
    summary.queued.map(({ alignmentId }) =>
      fetch(modalUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-webhook-secret": modalSecret,
        },
        body: JSON.stringify({ alignment_id: alignmentId }),
      }),
    ),
  );
  results.forEach((r, i) => {
    if (r.status === "rejected" || !r.value.ok) {
      console.error(
        `[song-sync] trigger failed for alignment ${summary.queued[i].alignmentId}:`,
        r.status === "rejected" ? r.reason : r.value.status,
      );
    }
  });
  summary.triggered = true;
  return summary;
}
