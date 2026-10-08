import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/songs/[id]/align-lyrics ───────────────────────────────────────
// Force-align known lyrics to a song master — the clean mix gives wav2vec2 far
// better input than a clip's phone audio. Same worker as
// POST /api/clips/[id]/align-lyrics, reading the song's audio instead. The
// song is transcribed once on first use (cached at songs.transcript_r2_key)
// purely to anchor the alignment windows; every caption word comes from the
// submitted lyrics.
//
// Returns 202 { alignmentId }. Poll GET until 'ready': `result.lines` is
// [{text, start, end, score}] in SONG time. Put the lines on a cut with
// POST /api/stories/[id]/lyric-captions { alignment_id }, which converts song
// time to output time through the cut's music bed. See docs/lyric_alignment.md.
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
  const { id: songId } = await context.params;

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

  const { data: song } = await supabase
    .from("songs")
    .select("id, status")
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

  const { data: alignment, error: insertError } = await supabase
    .from("lyric_alignments")
    .insert({
      song_id: songId,
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
    // Awaited (the endpoint only enqueues): a dropped fire-and-forget would
    // leave the row 'aligning' forever.
    try {
      const res = await fetch(modalUrl, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-webhook-secret": modalSecret,
        },
        body: JSON.stringify({ alignment_id: alignment.id }),
      });
      if (!res.ok) {
        console.error(`[song-align-lyrics] Modal returned ${res.status}`);
      }
    } catch (err) {
      console.error("[song-align-lyrics] Modal trigger failed:", err);
    }
  } else {
    console.warn("[song-align-lyrics] MODAL_LYRIC_ALIGN_URL not set — not triggered");
  }

  return NextResponse.json(
    { alignmentId: alignment.id, status: "accepted" },
    { status: 202 },
  );
}

// ── GET /api/songs/[id]/align-lyrics ────────────────────────────────────────
// List this song's lyric alignments (poll for status/result), newest first.
export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: songId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data, error } = await supabase
    .from("lyric_alignments")
    .select("id, status, result, error, created_at")
    .eq("song_id", songId)
    .order("created_at", { ascending: false });
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ alignments: data ?? [] });
}
