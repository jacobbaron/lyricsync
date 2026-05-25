import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { presignDownload } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── GET /api/stories/[id]/signed-url ──────────────────────────────────────
// Returns short-lived presigned R2 URLs for playback and download.
// Both expire after 1 hour — long enough for a viewing session.
//
// Response: { playback_url: string, download_url: string }

const TTL = 3600; // 1 hour

export async function GET(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: storyId } = await context.params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { data: story } = await supabase
    .from("stories")
    .select("id, status, render_r2_key")
    .eq("id", storyId)
    .maybeSingle();

  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }
  if (story.status !== "done" || !story.render_r2_key) {
    return NextResponse.json(
      { error: "Story is not ready yet" },
      { status: 409 },
    );
  }

  const [playback_url, download_url] = await Promise.all([
    presignDownload(story.render_r2_key, TTL),
    presignDownload(
      story.render_r2_key,
      TTL,
      'attachment; filename="output.mp4"',
    ),
  ]);

  return NextResponse.json({ playback_url, download_url });
}
