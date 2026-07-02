import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { requireClipWithVideo } from "@/lib/api/clips";
import { deferModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/analyze ──────────────────────────────────────────
// VIS-01 (dev): kicks off a Gemini visual-analysis run for a clip.
//
// Optional body: { "variant": "flash" | "flash_lowres" | "pro" | "editorial" }
//   Defaults to "flash". Each call creates a new visual_analyses row, so the
//   same clip can be analyzed under several variants and compared.
//
// Returns the created analysis id; poll GET /api/clips/[id]/visual for results.
//
// API-key callable (Authorization: Bearer lsk_...) like the render endpoints,
// so the whole upload → analyze → inspect → crop → render loop is scriptable.

// Keep in sync with VISUAL_VARIANTS in modal/app.py. "pro" is disabled there.
const VARIANTS = [
  "context",
  "flash",
  "flash_lowres",
  "editorial",
  "audio_aware",
  "with_transcript",
  // PERCEPTION T3: with_transcript grounded on the clip's QC + camera-motion
  // signals (see modal/app.py VISUAL_VARIANTS).
  "grounded",
] as const;
type Variant = (typeof VARIANTS)[number];

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Optional variant from body; default to "flash".
  let variant: Variant = "flash";
  try {
    const body = await request.json();
    if (body?.variant != null) {
      if (!VARIANTS.includes(body.variant)) {
        return NextResponse.json(
          { error: `variant must be one of: ${VARIANTS.join(", ")}` },
          { status: 400 },
        );
      }
      variant = body.variant;
    }
  } catch {
    // No body — fine, use the default variant.
  }

  const clip = await requireClipWithVideo(auth.supabase, clipId);
  if (clip instanceof NextResponse) return clip;

  // Create the analysis row up front so the worker (and the caller) have an id.
  const { data: analysis, error } = await auth.supabase
    .from("visual_analyses")
    .insert({ clip_id: clipId, variant, status: "analyzing" })
    .select("id")
    .single();

  if (error || !analysis) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create analysis" },
      { status: 500 },
    );
  }

  deferModal(
    "analyze",
    process.env.MODAL_ANALYZE_URL ??
      "https://jacobbaron--lyricsync-analyze-visuals.modal.run",
    { analysis_id: analysis.id },
  );

  return NextResponse.json(
    { id: analysis.id, variant, status: "analyzing" },
    { status: 202 },
  );
}
