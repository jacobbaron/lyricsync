import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { triggerModal } from "@/lib/modal/trigger";

export const runtime = "nodejs";

// ── GET /api/projects/[id]/stories ────────────────────────────────────────
// Returns all generation rounds with their stories, oldest-first.
// Stories within each round are ordered by creation time.
//
// Response: { rounds: [{ id, round, prompt, created_at, stories: [...] }] }

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

  const { data: rounds, error: roundsError } = await supabase
    .from("generation_rounds")
    .select("id, round, prompt, created_at")
    .eq("project_id", projectId)
    .order("round", { ascending: true });

  if (roundsError) {
    return NextResponse.json({ error: roundsError.message }, { status: 500 });
  }

  if (!rounds || rounds.length === 0) {
    return NextResponse.json({ rounds: [] });
  }

  const roundIds = rounds.map((r) => r.id);
  const { data: stories, error: storiesError } = await supabase
    .from("stories")
    .select(
      "id, generation_round_id, title, description, " +
        "estimated_duration_secs, ranges_json, status, error_message, created_at",
    )
    .in("generation_round_id", roundIds)
    .order("created_at", { ascending: true });

  if (storiesError) {
    return NextResponse.json({ error: storiesError.message }, { status: 500 });
  }

  type StoryRow = {
    id: string;
    generation_round_id: string;
    title: string | null;
    description: string | null;
    estimated_duration_secs: number | null;
    ranges_json: unknown;
    status: string;
    error_message: string | null;
    created_at: string;
  };

  const byRound: Record<string, StoryRow[]> = {};
  for (const s of (stories ?? []) as unknown as StoryRow[]) {
    (byRound[s.generation_round_id] ??= []).push(s);
  }

  return NextResponse.json({
    rounds: rounds.map((r) => ({ ...r, stories: byRound[r.id] ?? [] })),
  });
}

// ── types ──────────────────────────────────────────────────────────────────

interface Range {
  source: string;
  start: number;
  end: number;
}

// ── POST /api/projects/[id]/stories ────────────────────────────────────────
// Creates a new story row with the given ranges and fires the render task.
// Body: { ranges: [{ source, start, end }], title?: string, description?: string }
// title/description are optional; when omitted the cut shows as "Untitled cut".

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  // Parse body
  let ranges: Range[];
  // Optional human-readable label for the cut. Without it, API-created cuts
  // show as "Untitled cut" in the web UI — generation-flow stories get a title
  // from Claude, but direct API creates have no other write path for it.
  let title: string | null = null;
  let description: string | null = null;
  try {
    const body = await request.json();
    ranges = body.ranges;
    if (!Array.isArray(ranges) || ranges.length === 0) {
      return NextResponse.json(
        { error: "ranges must be a non-empty array" },
        { status: 400 },
      );
    }
    for (const r of ranges) {
      if (
        typeof r.source !== "string" ||
        typeof r.start !== "number" ||
        typeof r.end !== "number" ||
        r.end <= r.start
      ) {
        return NextResponse.json(
          { error: "each range must have source, start, and end (end > start)" },
          { status: 400 },
        );
      }
    }
    // Optional title / description — must be strings when present. Empty/
    // whitespace-only values are treated as omitted (stored as null).
    if (body.title != null) {
      if (typeof body.title !== "string") {
        return NextResponse.json(
          { error: "title must be a string" },
          { status: 400 },
        );
      }
      title = body.title.trim() || null;
    }
    if (body.description != null) {
      if (typeof body.description !== "string") {
        return NextResponse.json(
          { error: "description must be a string" },
          { status: 400 },
        );
      }
      description = body.description.trim() || null;
    }
  } catch {
    return NextResponse.json({ error: "Invalid JSON body" }, { status: 400 });
  }

  // Verify project exists and belongs to user (RLS enforces this too)
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Create the story row
  const { data: story, error } = await supabase
    .from("stories")
    .insert({
      project_id: projectId,
      ranges_json: ranges,
      status: "rendering",
      title,
      description,
    })
    .select("id")
    .single();

  if (error || !story) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create story" },
      { status: 500 },
    );
  }

  // Note: project status intentionally NOT updated here — the project stays
  // at 'transcribed' so the range picker remains available for new cuts.
  // Individual stories track their own rendering lifecycle.

  triggerModal("stories", process.env.MODAL_RENDER_URL, {
    project_id: projectId,
    story_id: story.id,
  });

  return NextResponse.json({ id: story.id }, { status: 201 });
}
