import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/songs/[id]/complete ───────────────────────────────────────────
// Called after the client PUTs the audio to the presigned URL. Marks the song
// 'ready' so it can be aligned/rendered against. Optional duration_secs (the
// client already has the file and can probe it) is stored for display.

const Body = z
  .object({ durationSecs: z.number().positive().max(36000).optional() })
  .default({});

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: songId } = await context.params;

  const parsed = Body.safeParse(await request.json().catch(() => ({})));
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

  // RLS scopes this to songs in the caller's projects.
  const { data: song } = await supabase
    .from("songs")
    .select("id, r2_key")
    .eq("id", songId)
    .maybeSingle();
  if (!song) {
    return NextResponse.json({ error: "Song not found" }, { status: 404 });
  }
  if (!song.r2_key) {
    return NextResponse.json(
      { error: "Song has no r2_key — re-register the upload" },
      { status: 409 },
    );
  }

  const update: Record<string, unknown> = { status: "ready" };
  if (parsed.data.durationSecs !== undefined) {
    update.duration_secs = parsed.data.durationSecs;
  }
  const { error } = await supabase
    .from("songs")
    .update(update)
    .eq("id", songId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ songId, status: "ready" });
}
