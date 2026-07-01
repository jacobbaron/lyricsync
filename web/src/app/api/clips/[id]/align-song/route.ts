import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/align-song ─────────────────────────────────────────
// Compute a durable clip↔song alignment for a footage window (chroma-DTW on
// Modal). Creates a clip_alignments row (status 'aligning'), fires the worker,
// and returns 202. Poll GET to see it become 'ready' with song_start set. The
// alignment is reusable metadata — a cut later uses it to lip-sync this footage
// to a music bed of the same song (POST /api/stories/[id]/lipsync).
//
// Body: { song_id, start, end } — footage-local seconds of the take to align.

const Body = z
  .object({
    song_id: z.string().uuid(),
    start: z.number().min(0).max(36000),
    end: z.number().min(0).max(36000),
  })
  .refine((b) => b.end > b.start, { message: "end must be greater than start" });

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

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

  const { data: clip } = await supabase
    .from("clips")
    .select("id, project_id")
    .eq("id", clipId)
    .maybeSingle();
  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  const { data: song } = await supabase
    .from("songs")
    .select("id, project_id, status")
    .eq("id", parsed.data.song_id)
    .maybeSingle();
  if (!song || song.project_id !== clip.project_id) {
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

  const { data: alignment, error: insertError } = await supabase
    .from("clip_alignments")
    .insert({
      clip_id: clipId,
      song_id: parsed.data.song_id,
      footage_start: parsed.data.start,
      footage_end: parsed.data.end,
      status: "aligning",
    })
    .select("id")
    .single();
  if (insertError || !alignment) {
    return NextResponse.json(
      { error: insertError?.message ?? "Failed to create alignment" },
      { status: 500 },
    );
  }

  const modalUrl = process.env.MODAL_MUSIC_ALIGN_URL;
  const modalSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (modalUrl && modalSecret) {
    fetch(modalUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": modalSecret,
      },
      body: JSON.stringify({ alignment_id: alignment.id }),
    }).catch((err) => console.error("[align-song] Modal trigger failed:", err));
  } else {
    console.warn("[align-song] MODAL_MUSIC_ALIGN_URL not set — not triggered");
  }

  return NextResponse.json(
    { alignmentId: alignment.id, status: "accepted" },
    { status: 202 },
  );
}

// ── GET /api/clips/[id]/align-song ──────────────────────────────────────────
// List this clip's alignments (poll for status/song_start), newest first.
export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data, error } = await supabase
    .from("clip_alignments")
    .select(
      "id, song_id, footage_start, footage_end, song_start, cost, status, error, created_at",
    )
    .eq("clip_id", clipId)
    .order("created_at", { ascending: false });
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ alignments: data ?? [] });
}
