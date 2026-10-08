import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { startSongSync } from "@/lib/songs/sync";

export const runtime = "nodejs";

// ── POST /api/songs/[id]/sync ───────────────────────────────────────────────
// (Re-)locate every clip of the song's project inside the song: one
// clip_alignments row per clip covering its full duration, aligned on Modal
// (same-take fast path with a drift check, falling back to chroma-DTW). Runs
// automatically on /complete; call it again after adding clips. Poll
// GET /api/projects/[id]/songs for per-clip results.
//
// Returns 202 { queued: [{clipId, alignmentId}], skipped: [{clipId, reason}] }.

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: songId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data: song } = await supabase
    .from("songs")
    .select("id, project_id, status")
    .eq("id", songId)
    .maybeSingle();
  if (!song) {
    return NextResponse.json({ error: "Song not found" }, { status: 404 });
  }
  if (song.status !== "ready") {
    return NextResponse.json(
      { error: `Song not ready (status: ${song.status})` },
      { status: 409 },
    );
  }

  try {
    const summary = await startSongSync(supabase, song);
    return NextResponse.json(summary, { status: 202 });
  } catch (err) {
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "sync failed" },
      { status: 500 },
    );
  }
}
