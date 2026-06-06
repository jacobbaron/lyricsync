import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

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

const VARIANTS = ["flash", "flash_lowres", "pro", "editorial"] as const;
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
  const { supabase } = auth;

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

  // Verify clip exists and is accessible (RLS enforces ownership via project).
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

  // Create the analysis row up front so the worker (and the caller) have an id.
  const { data: analysis, error } = await supabase
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

  // Schedule the Modal call via after() so the 202 is returned immediately and
  // Vercel keeps the function alive to complete the fetch after the response.
  //
  // Plain await times out on a cold-start of the analyze_image container (a
  // separate Modal image from the main one used by transcribe/render, so it has
  // no warm pool). after() solves both problems: the caller isn't blocked, and
  // the fetch actually completes (unlike naked fire-and-forget which is killed
  // the moment the response goes out).
  //
  // The URL falls back to the known deployed endpoint so the VIS-01 harness
  // works without an extra Vercel env var. Set MODAL_ANALYZE_URL to override.
  const analyzeUrl =
    process.env.MODAL_ANALYZE_URL ??
    "https://jacobbaron--lyricsync-analyze-visuals.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (!webhookSecret) {
    console.warn("[analyze] MODAL_WEBHOOK_SECRET not set — analysis not triggered");
  } else {
    const analysisId = analysis.id;
    after(async () => {
      try {
        const res = await fetch(analyzeUrl, {
          method: "POST",
          headers: {
            "Content-Type": "application/json",
            "x-webhook-secret": webhookSecret,
          },
          body: JSON.stringify({ analysis_id: analysisId }),
        });
        if (!res.ok) {
          const text = await res.text().catch(() => "");
          console.error(`[analyze] Modal returned ${res.status}: ${text}`);
        }
      } catch (err) {
        console.error("[analyze] Modal trigger failed:", err);
      }
    });
  }

  return NextResponse.json(
    { id: analysis.id, variant, status: "analyzing" },
    { status: 202 },
  );
}
