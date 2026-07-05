import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/clips/[id]/analyze ──────────────────────────────────────────
// Kicks off the canonical Gemini visual-analysis run for a clip.
//
// Optional body: { "variant": "v2" }
//   Defaults to "v2" (the unified analysis) — the only supported variant. The
//   A/B-era variant names still map to "v2" for a deprecation window (see
//   DEPRECATED_VARIANTS) so existing scripts keep working; the response then
//   carries a `Warning` header. Each call creates a new visual_analyses row.
//
// Returns the created analysis id; poll GET /api/clips/[id]/visual for results.
//
// API-key callable (Authorization: Bearer lsk_...) like the render endpoints,
// so the whole upload → analyze → inspect → crop → render loop is scriptable.

// The one supported variant. Keep in sync with VISUAL_VARIANTS in modal/app.py.
const CANONICAL_VARIANT = "v2";

// A2 (#110): retired A/B-era variants. Still accepted so existing scripts don't
// 400, but they're transparently mapped to the unified CANONICAL_VARIANT and the
// response gets a deprecation `Warning` header. Remove after the migration window.
const DEPRECATED_VARIANTS = new Set([
  "context",
  "flash",
  "flash_lowres",
  "pro",
  "editorial",
  "audio_aware",
  "with_transcript",
  "grounded",
]);

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

  // Optional variant from body; default to the canonical unified analysis.
  // Deprecated A/B-era names are accepted but mapped to the canonical variant
  // (and flagged with a Warning header) rather than rejected.
  const variant = CANONICAL_VARIANT;
  let deprecatedVariant: string | null = null;
  try {
    const body = await request.json();
    if (body?.variant != null) {
      const requested = String(body.variant);
      if (requested === CANONICAL_VARIANT) {
        // canonical — nothing to do
      } else if (DEPRECATED_VARIANTS.has(requested)) {
        deprecatedVariant = requested;
      } else {
        return NextResponse.json(
          { error: `variant must be "${CANONICAL_VARIANT}"` },
          { status: 400 },
        );
      }
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
        const text = await res.text().catch(() => "");
        console.log(`[analyze] Modal responded ${res.status}: ${text}`);
        if (!res.ok) {
          console.error(`[analyze] Modal error for ${analysisId}: ${text}`);
        }
      } catch (err) {
        console.error("[analyze] Modal trigger failed:", err);
      }
    });
  }

  const headers: Record<string, string> = {};
  if (deprecatedVariant) {
    // RFC 7234 warn-code 299 ("miscellaneous persistent warning").
    headers["Warning"] = `299 - "variant '${deprecatedVariant}' is deprecated; mapped to '${CANONICAL_VARIANT}'"`;
  }

  return NextResponse.json(
    { id: analysis.id, variant, status: "analyzing" },
    { status: 202, headers },
  );
}
