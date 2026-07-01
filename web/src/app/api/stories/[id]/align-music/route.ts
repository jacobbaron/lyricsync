import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/align-music ──────────────────────────────────────
// Auto-align this story's footage to a project song (chroma-DTW on Modal), then
// re-render with the finished track under the footage. The worker computes the
// offset, writes stories.music_json, and fires the render — so this returns 202
// and the story advances to 'done' when the render finishes.
//
// Body: { song_id }.

const Body = z.object({ song_id: z.string().uuid() });

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
    .select("id, project_id")
    .eq("id", storyId)
    .maybeSingle();
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }

  // Song must exist in the same project and be uploaded.
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

  await supabase
    .from("stories")
    .update({ status: "rendering", error_message: null })
    .eq("id", storyId);

  const modalUrl = process.env.MODAL_MUSIC_ALIGN_URL;
  const modalSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!modalUrl || !modalSecret) {
    console.warn(
      "[align-music] MODAL_MUSIC_ALIGN_URL not set — alignment not triggered",
    );
    return NextResponse.json({ status: "accepted", modal: false }, { status: 202 });
  }

  try {
    const res = await fetch(modalUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": modalSecret,
      },
      body: JSON.stringify({ story_id: storyId, song_id: parsed.data.song_id }),
    });
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      console.error(`[align-music] Modal returned ${res.status}: ${text}`);
    }
  } catch (err) {
    console.error("[align-music] Failed to reach Modal endpoint:", err);
  }

  return NextResponse.json({ status: "accepted" }, { status: 202 });
}
