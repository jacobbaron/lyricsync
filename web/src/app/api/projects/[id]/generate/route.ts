import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";

// ── POST /api/projects/[id]/generate ──────────────────────────────────────
// Creates a new generation round, inserts 3 placeholder story rows, updates
// the project to 'generating_stories', and fires the Modal generate endpoint.
// Returns { round_id } immediately — the worker fills in stories async.

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Parse optional prompt from body
  let prompt: string | null = null;
  try {
    const body = await request.json().catch(() => ({}));
    prompt = typeof body.prompt === "string" && body.prompt.trim()
      ? body.prompt.trim()
      : null;
  } catch {
    // empty body is fine
  }

  // Verify project ownership (RLS also enforces this)
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  const allowedStatuses = ["transcribed", "stories_ready", "error"];
  if (!allowedStatuses.includes(project.status)) {
    return NextResponse.json(
      { error: `Cannot generate stories from status '${project.status}'` },
      { status: 409 },
    );
  }

  // Determine round number (count existing rounds + 1)
  const { count } = await supabase
    .from("generation_rounds")
    .select("id", { count: "exact", head: true })
    .eq("project_id", projectId);

  const roundNumber = (count ?? 0) + 1;

  // Create the generation round row
  const { data: round, error: roundError } = await supabase
    .from("generation_rounds")
    .insert({ project_id: projectId, round: roundNumber, prompt })
    .select("id")
    .single();

  if (roundError || !round) {
    return NextResponse.json(
      { error: roundError?.message ?? "Failed to create generation round" },
      { status: 500 },
    );
  }

  // Create 3 placeholder story rows (status='generating')
  const placeholders = [1, 2, 3].map(() => ({
    project_id: projectId,
    generation_round_id: round.id,
    status: "generating",
  }));

  const { error: storiesError } = await supabase
    .from("stories")
    .insert(placeholders);

  if (storiesError) {
    return NextResponse.json(
      { error: storiesError.message },
      { status: 500 },
    );
  }

  // Advance project status
  await supabase
    .from("projects")
    .update({ status: "generating_stories", story_prompt: prompt })
    .eq("id", projectId);

  // Fire Modal (fire-and-forget)
  const generateUrl = process.env.MODAL_GENERATE_URL;
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
  if (generateUrl && webhookSecret) {
    fetch(generateUrl, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "x-webhook-secret": webhookSecret,
      },
      body: JSON.stringify({ project_id: projectId, round_id: round.id }),
    }).catch((err) => console.error("[generate] Modal trigger failed:", err));
  } else {
    console.warn("[generate] MODAL_GENERATE_URL not set — generation not triggered");
  }

  return NextResponse.json({ round_id: round.id }, { status: 202 });
}
