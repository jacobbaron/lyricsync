import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { presignDownload } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── GET /api/clips/[id]/signals ─────────────────────────────────────────────
// PERCEPTION T1+: returns every clip_signals row for a clip (quality, and
// later camera_motion / detection / ...), newest first. Presigns
// result_r2_key into a download url so the caller can pull the full
// per-second/per-frame sidecar without a second round trip to R2 directly.
//
// Query params:
//   ?kind=quality   — only rows of that kind
//
// Response: { clip: {...}, signals: [{ id, kind, status, result, sidecar_url,
//   error, created_at }] }

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
  const kind = url.searchParams.get("kind");

  const { data: clip } = await supabase
    .from("clips")
    .select("id, filename, duration_secs, status")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  let query = supabase
    .from("clip_signals")
    .select("id, kind, status, result, result_r2_key, error, created_at")
    .eq("clip_id", clipId)
    .order("created_at", { ascending: false });

  if (kind) query = query.eq("kind", kind);

  const { data: rows, error } = await query;
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  const signals = await Promise.all(
    (rows ?? []).map(async (row) => {
      const { result_r2_key, ...rest } = row;
      return {
        ...rest,
        sidecar_url: result_r2_key
          ? await presignDownload(result_r2_key, 3600)
          : null,
      };
    }),
  );

  return NextResponse.json({ clip, signals });
}
