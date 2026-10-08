import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { deleteObjects } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── DELETE /api/songs/[id] ──────────────────────────────────────────────────
// Remove a song: its R2 audio (and cached transcript), then the row. Its
// clip_alignments and lyric_alignments go with it (on delete cascade). Cuts
// whose music bed is this song have the bed cleared (not re-rendered), so the
// next render doesn't fail on a missing song.

export async function DELETE(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: songId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  // RLS scopes this to songs in the caller's projects.
  const { data: song } = await supabase
    .from("songs")
    .select("id, r2_key, transcript_r2_key")
    .eq("id", songId)
    .maybeSingle();
  if (!song) {
    return NextResponse.json({ error: "Song not found" }, { status: 404 });
  }

  const { error: bedError } = await supabase
    .from("stories")
    .update({ music_json: null })
    .eq("music_json->>song_id", songId);
  if (bedError) {
    return NextResponse.json({ error: bedError.message }, { status: 500 });
  }

  // Purge storage before the row so deleting never orphans objects.
  await deleteObjects([song.r2_key, song.transcript_r2_key]);

  const { error } = await supabase.from("songs").delete().eq("id", songId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return new NextResponse(null, { status: 204 });
}
