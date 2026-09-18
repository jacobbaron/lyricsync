import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/align-lyrics ───────────────────────────────────────
// Force-align known lyrics to a clip's audio (wav2vec2 on Modal). The caller
// supplies the text; the worker solves only for timing, so nothing is invented
// by ASR — which is the only approach that works on sung audio, where
// transcription mishears words, drops lines under loud instrumentation, and
// loops on repeated refrains.
//
// Creates a lyric_alignments row (status 'aligning'), fires the worker, and
// returns 202. Poll GET until it becomes 'ready' with `result.lines` —
// [{text, start, end, score}] in clip-local seconds, ready to drop onto a
// timeline text track. See docs/lyric_alignment.md.
//
// Body: { lyrics } — one caption line per line; blank lines and bracketed
// section headings ("[Chorus]") are ignored.

const MAX_LYRICS_CHARS = 20000;

const Body = z.object({
  lyrics: z.string().min(1).max(MAX_LYRICS_CHARS),
});

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
    .select("id, status")
    .eq("id", clipId)
    .maybeSingle();
  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  const { data: alignment, error: insertError } = await supabase
    .from("lyric_alignments")
    .insert({
      clip_id: clipId,
      lyrics: parsed.data.lyrics,
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

  const modalUrl = process.env.MODAL_LYRIC_ALIGN_URL;
  const modalSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (modalUrl && modalSecret) {
    fetch(modalUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": modalSecret,
      },
      body: JSON.stringify({ alignment_id: alignment.id }),
    }).catch((err) =>
      console.error("[align-lyrics] Modal trigger failed:", err),
    );
  } else {
    console.warn("[align-lyrics] MODAL_LYRIC_ALIGN_URL not set — not triggered");
  }

  return NextResponse.json(
    { alignmentId: alignment.id, status: "accepted" },
    { status: 202 },
  );
}

// ── GET /api/clips/[id]/align-lyrics ────────────────────────────────────────
// List this clip's lyric alignments (poll for status/result), newest first.
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
    .from("lyric_alignments")
    .select("id, status, result, error, created_at")
    .eq("clip_id", clipId)
    .order("created_at", { ascending: false });
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ alignments: data ?? [] });
}
