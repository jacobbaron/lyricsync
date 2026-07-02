import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";

type ClipWithVideo = { id: string; r2_key: string };

/**
 * Looks up a clip by id, verifying it exists and has an uploaded video (r2_key).
 * Returns the clip row on success, or a NextResponse error (404 / 409) to
 * return directly from the route handler.
 */
export async function requireClipWithVideo(
  supabase: SupabaseClient,
  clipId: string,
): Promise<ClipWithVideo | NextResponse> {
  const { data: clip } = await supabase
    .from("clips")
    .select("id, r2_key")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }
  if (!clip.r2_key) {
    return NextResponse.json(
      { error: "Clip has no uploaded video yet" },
      { status: 409 },
    );
  }

  return clip as ClipWithVideo;
}
