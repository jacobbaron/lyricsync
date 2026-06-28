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
import { ContactSheetQuery } from "./schemas";

export const runtime = "nodejs";
export const maxDuration = 300;

// ── GET /api/clips/[id]/contact-sheet?start=&end=&cols=&rows= ──────────────
// Interactive perception (roadmap §1.3): sample cols*rows frames across a
// range, burn the timestamp into each, and tile them into ONE jpeg — one image
// for a multimodal model to read instead of N. Cached by
// (clip_id, start, end, cols, rows).
//
// API-key callable (Authorization: Bearer lsk_…).

const SHEET_TTL = 3600;

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
  const parsed = ContactSheetQuery.safeParse(
    Object.fromEntries(url.searchParams),
  );
  if (!parsed.success) {
    return NextResponse.json(
      { error: parsed.error.issues.map((i) => i.message).join("; ") },
      { status: 400 },
    );
  }
  const { start, end, cols, rows } = parsed.data;

  const { data: clip } = await supabase
    .from("clips")
    .select("id")
    .eq("id", clipId)
    .maybeSingle();
  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  // `end` may be undefined (defaults to clip duration in Modal). Only key on it
  // when supplied, so the "whole clip" sheet has a stable key.
  const params: Record<string, unknown> = { start, cols, rows };
  if (end !== undefined) params.end = end;
  const key = cacheKey(clipId, "contact_sheet", params);

  const cached = await readCache(supabase, key);
  if (cached?.key) {
    return NextResponse.json({
      clip_id: clipId,
      start: cached.start ?? start,
      end: cached.end ?? end ?? null,
      cols,
      rows,
      url: await presignDownload(cached.key as string, SHEET_TTL),
      cached: true,
    });
  }

  let result: {
    start: number;
    end: number;
    cols: number;
    rows: number;
    key: string;
  };
  try {
    result = await callPerception("contact_sheet", {
      clip_id: clipId,
      start,
      end,
      cols,
      rows,
    });
  } catch (err) {
    if (err instanceof PerceptionError) {
      return NextResponse.json({ error: err.message }, { status: err.status });
    }
    throw err;
  }

  await writeCache(supabase, clipId, "contact_sheet", key, params, {
    key: result.key,
    start: result.start,
    end: result.end,
  });

  return NextResponse.json({
    clip_id: clipId,
    start: result.start,
    end: result.end,
    cols: result.cols,
    rows: result.rows,
    url: await presignDownload(result.key, SHEET_TTL),
    cached: false,
  });
}
