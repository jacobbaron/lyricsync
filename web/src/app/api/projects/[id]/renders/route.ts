import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── GET /api/projects/[id]/renders ────────────────────────────────────────
// Lists "direct" renders — stories created outside the story-generation flow
// (generation_round_id IS NULL), e.g. cuts produced through the REST API.
// These never appear under "Story Options" (which only groups stories by
// generation round), so this is how the web UI surfaces them for download.
//
// Newest-first. Response: { renders: [{ id, title, status, error_message,
//   duration_secs, source_count, created_at }] }

type RangeRow = { source?: string; start?: number; end?: number };

export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  const { data: stories, error } = await supabase
    .from("stories")
    .select(
      "id, title, status, error_message, estimated_duration_secs, " +
        "ranges_json, created_at",
    )
    .eq("project_id", projectId)
    .is("generation_round_id", null)
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  type StoryRow = {
    id: string;
    title: string | null;
    status: string;
    error_message: string | null;
    estimated_duration_secs: number | null;
    ranges_json: unknown;
    created_at: string;
  };

  const renders = ((stories ?? []) as unknown as StoryRow[]).map((s) => {
    const ranges = Array.isArray(s.ranges_json)
      ? (s.ranges_json as RangeRow[])
      : [];
    // Fall back to summing the cut ranges when no estimate was stored —
    // API-created stories don't carry an estimated_duration_secs.
    const durationFromRanges = ranges.reduce(
      (acc, r) =>
        typeof r.start === "number" && typeof r.end === "number" && r.end > r.start
          ? acc + (r.end - r.start)
          : acc,
      0,
    );
    return {
      id: s.id,
      title: s.title,
      status: s.status,
      error_message: s.error_message,
      duration_secs: s.estimated_duration_secs ?? (durationFromRanges || null),
      source_count: ranges.length,
      created_at: s.created_at,
    };
  });

  return NextResponse.json({ renders });
}
