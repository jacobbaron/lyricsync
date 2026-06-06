import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/clips/[id]/visual ────────────────────────────────────────────
// VIS-01 (dev): returns every visual-analysis run for a clip, newest first.
//
// Deliberately verbose for diagnosis while this is in development: each run
// includes the parsed `result` AND the full `debug` blob (prompt, raw Gemini
// response, token usage, timings, file-processing state, traceback on error).
//
// Query params:
//   ?variant=flash   — only runs for that variant
//   ?debug=0         — omit the (large) debug blob, return just results
//
// Response: { clip: {...}, analyses: [{ id, variant, status, result, debug?, error, created_at }] }

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

  const url = new URL(request.url);
  const variant = url.searchParams.get("variant");
  const includeDebug = url.searchParams.get("debug") !== "0";

  const { data: clip } = await supabase
    .from("clips")
    .select("id, filename, duration_secs, status, r2_key")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  const columns = includeDebug
    ? "id, variant, status, result, result_r2_key, debug, error, created_at"
    : "id, variant, status, result, result_r2_key, error, created_at";

  let query = supabase
    .from("visual_analyses")
    .select(columns)
    .eq("clip_id", clipId)
    .order("created_at", { ascending: false });

  if (variant) query = query.eq("variant", variant);

  const { data: analyses, error } = await query;
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json({ clip, analyses: analyses ?? [] });
}
