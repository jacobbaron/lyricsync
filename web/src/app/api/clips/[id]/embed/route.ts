import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { requireClipWithVideo } from "@/lib/api/clips";
import { createClipSignal } from "@/lib/api/signals";
import { deferModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/embed ──────────────────────────────────────────────
// PERCEPTION T4: kicks off a clip-embedding run — CLIP-encode ~1 fps frames
// into pgvector (clip_embeddings) for semantic search. Creates a clip_signals
// row (kind=embedding) up front to track status and fires the Modal worker;
// poll GET /api/clips/[id]/signals?kind=embedding. Search the results with
// GET /api/projects/[id]/search?q=...
//
// API-key callable (Authorization: Bearer lsk_...).

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const clip = await requireClipWithVideo(auth.supabase, clipId);
  if (clip instanceof NextResponse) return clip;

  const signal = await createClipSignal(auth.supabase, clipId, "embedding");
  if (signal instanceof NextResponse) return signal;

  deferModal(
    "embed",
    process.env.MODAL_EMBED_URL ??
      "https://jacobbaron--lyricsync-embed-clip.modal.run",
    { signal_id: signal.id },
  );

  return NextResponse.json(
    { id: signal.id, kind: "embedding", status: "processing" },
    { status: 202 },
  );
}
