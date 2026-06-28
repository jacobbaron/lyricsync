import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import {
  callPerception,
  cacheKey,
  PerceptionError,
  readCache,
  writeCache,
} from "@/lib/perception/modal";
import { DescribeBody } from "./schemas";

export const runtime = "nodejs";
// Gemini trim + upload + generate can take a while on a long source download.
export const maxDuration = 300;

// ── POST /api/clips/[id]/describe {start, end, question?} ───────────────────
// Interactive perception (roadmap §1.3): ask Gemini Flash about JUST a
// sub-range of a clip, frame-accurate, instead of relying on a coarse
// whole-clip analysis. ffmpeg trims [start, end] → Gemini answers `question`
// (or a sensible default). Persisted/cached in clip_inspections by
// (clip_id, start, end, normalized question) for cheap repeats + audit.
//
// API-key callable (Authorization: Bearer lsk_…).

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

  let raw: unknown;
  try {
    raw = await request.json();
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }
  const parsed = DescribeBody.safeParse(raw);
  if (!parsed.success) {
    return NextResponse.json(
      { error: parsed.error.issues.map((i) => i.message).join("; ") },
      { status: 400 },
    );
  }
  const { start, end, question } = parsed.data;
  if (end <= start) {
    return NextResponse.json(
      { error: "end must be greater than start" },
      { status: 400 },
    );
  }

  const { data: clip } = await supabase
    .from("clips")
    .select("id")
    .eq("id", clipId)
    .maybeSingle();
  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  // Normalize the question for the cache key (trim/lowercase) so trivially
  // different phrasings of the same ask still hit. Empty → the Modal default.
  const normQuestion = (question ?? "").trim().toLowerCase();
  const params = {
    start: Number(start.toFixed(3)),
    end: Number(end.toFixed(3)),
    question: normQuestion,
  };
  const key = cacheKey(clipId, "describe", params);

  const cached = await readCache(supabase, key);
  if (cached?.answer != null) {
    return NextResponse.json({
      clip_id: clipId,
      start: params.start,
      end: params.end,
      answer: cached.answer,
      model: cached.model ?? "gemini-3.5-flash",
      cached: true,
    });
  }

  let result: { start: number; end: number; answer: string; model: string };
  try {
    result = await callPerception("describe", {
      clip_id: clipId,
      start,
      end,
      question,
    });
  } catch (err) {
    if (err instanceof PerceptionError) {
      return NextResponse.json({ error: err.message }, { status: err.status });
    }
    throw err;
  }

  await writeCache(supabase, clipId, "describe", key, params, {
    answer: result.answer,
    model: result.model,
  });

  return NextResponse.json({
    clip_id: clipId,
    start: result.start,
    end: result.end,
    answer: result.answer,
    model: result.model,
    cached: false,
  });
}
