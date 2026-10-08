import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";
import { type Bed, cutDuration, triggerRender } from "@/lib/songs/bed";
import { type LyricLine, lyricCaptionOps } from "@/lib/songs/captions";

export const runtime = "nodejs";

// ── POST /api/stories/[id]/lyric-captions ───────────────────────────────────
// One step from a song-level lyric alignment to captions on a cut: converts
// each line from song time to output time through the cut's music bed
// (output_t = song_t - bed.song_start), drops lines outside the cut, applies
// them as add_text ops through the timeline edit service, and re-renders.
//
// Body: { alignment_id, style?, dry_run? }
//   alignment_id  a ready lyric alignment of the bed's song
//                 (POST /api/songs/[id]/align-lyrics)
//   style         optional add_text fields overriding the default lyric look
//                 (size, position, wrap, color, outline, box_opacity, fade, …)
//   dry_run       true → return the ops without applying or rendering
//
// Captions are added, not replaced: remove earlier ones with remove_text ops
// (POST /api/stories/[id]/edit) before re-running.

const Body = z.object({
  alignment_id: z.string().uuid(),
  style: z.record(z.unknown()).optional(),
  dry_run: z.boolean().optional(),
});

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
    .select("id, ranges_json, timeline_json, music_json")
    .eq("id", storyId)
    .maybeSingle();
  if (!story) {
    return NextResponse.json({ error: "Story not found" }, { status: 404 });
  }
  const bed = story.music_json as Bed | null;
  if (!bed?.song_id || bed.song_start == null) {
    return NextResponse.json(
      { error: "Set a music bed first (POST /api/stories/[id]/music)" },
      { status: 409 },
    );
  }

  const { data: alignment } = await supabase
    .from("lyric_alignments")
    .select("id, song_id, status, result")
    .eq("id", parsed.data.alignment_id)
    .maybeSingle();
  if (!alignment) {
    return NextResponse.json({ error: "Alignment not found" }, { status: 404 });
  }
  if (alignment.song_id !== bed.song_id) {
    return NextResponse.json(
      { error: "Alignment is not of this cut's bed song — align the lyrics against that song" },
      { status: 409 },
    );
  }
  if (alignment.status !== "ready") {
    return NextResponse.json(
      { error: `Alignment not ready (status: ${alignment.status})` },
      { status: 409 },
    );
  }

  const lines = ((alignment.result as { lines?: LyricLine[] } | null)?.lines ??
    []) as LyricLine[];
  const ops = lyricCaptionOps(
    lines,
    Number(bed.song_start),
    cutDuration(story),
    parsed.data.style,
  );
  if (parsed.data.dry_run) {
    return NextResponse.json({ ops, dropped: lines.length - ops.length });
  }
  if (ops.length === 0) {
    return NextResponse.json(
      { error: "No lyric lines fall inside the cut at the bed's song_start" },
      { status: 409 },
    );
  }

  // Same edit service as POST /api/stories/[id]/edit (see that route).
  const editUrl =
    process.env.MODAL_EDIT_URL ||
    process.env.MODAL_RENDER_URL?.replace("render-story", "edit-timeline");
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!editUrl || !webhookSecret) {
    return NextResponse.json(
      { error: "MODAL_EDIT_URL not configured (and could not derive it from MODAL_RENDER_URL)" },
      { status: 503 },
    );
  }
  const upstream = await fetch(editUrl, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-webhook-secret": webhookSecret,
    },
    body: JSON.stringify({
      story_id: storyId,
      ops,
      base_revision: null,
      restore_revision: null,
    }),
  });
  const payload = await upstream.json().catch(() => ({
    detail: "edit service returned a non-JSON response",
  }));
  if (!upstream.ok) {
    return NextResponse.json(payload, { status: upstream.status });
  }

  await supabase
    .from("stories")
    .update({ status: "rendering", error_message: null })
    .eq("id", storyId);
  triggerRender(storyId, "lyric-captions");

  return NextResponse.json({
    status: "accepted",
    added: ops.length,
    dropped: lines.length - ops.length,
    revision: (payload as { revision?: number }).revision ?? null,
  });
}
