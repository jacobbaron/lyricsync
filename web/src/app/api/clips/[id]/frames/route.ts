import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { presignDownload } from "@/lib/r2/client";
import {
  callPerception,
  cacheKey,
  PerceptionError,
  readCache,
  writeCache,
} from "@/lib/perception/modal";
import { FramesQuery } from "./schemas";

export const runtime = "nodejs";

// ── GET /api/clips/[id]/frames?t=&n=&interval= ─────────────────────────────
// Interactive perception (roadmap §1.3): extract N frames from a clip so an
// editing agent can *look* at exactly the moment it's unsure about instead of
// trusting whole-clip analysis or improvising a render-and-extract dance.
//
// Fast-seeks one frame at t, t+interval, … (n frames), stores each JPEG in R2,
// and returns signed URLs. Results are cached by (clip_id, t, n, interval) in
// clip_inspections; the underlying R2 frames are immutable per params.
//
// API-key callable (Authorization: Bearer lsk_…) like /analyze.

const FRAME_TTL = 3600;

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
  const parsed = FramesQuery.safeParse(Object.fromEntries(url.searchParams));
  if (!parsed.success) {
    return NextResponse.json(
      { error: parsed.error.issues.map((i) => i.message).join("; ") },
      { status: 400 },
    );
  }
  const { t, n, interval } = parsed.data;

  // RLS scopes this to the caller's clips, so a hit/miss is owner-safe.
  const { data: clip } = await supabase
    .from("clips")
    .select("id")
    .eq("id", clipId)
    .maybeSingle();
  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  const params = { t, n, interval };
  const key = cacheKey(clipId, "frames", params);

  // Cache hit: re-presign the stored (immutable) R2 keys.
  const cached = await readCache(supabase, key);
  if (cached?.frames) {
    const items = cached.frames as { t: number; key: string }[];
    const frames = await Promise.all(
      items.map(async (f) => ({
        t: f.t,
        url: await presignDownload(f.key, FRAME_TTL),
      })),
    );
    return NextResponse.json({ clip_id: clipId, frames, cached: true });
  }

  let modalResult: { frames: { t: number; key: string }[] };
  try {
    modalResult = await callPerception("frames", {
      clip_id: clipId,
      t,
      n,
      interval,
    });
  } catch (err) {
    if (err instanceof PerceptionError) {
      return NextResponse.json({ error: err.message }, { status: err.status });
    }
    throw err;
  }

  await writeCache(supabase, clipId, "frames", key, params, {
    frames: modalResult.frames,
  });

  const frames = await Promise.all(
    modalResult.frames.map(async (f) => ({
      t: f.t,
      url: await presignDownload(f.key, FRAME_TTL),
    })),
  );

  return NextResponse.json({ clip_id: clipId, frames, cached: false });
}
