import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { triggerModal } from "@/lib/modal/trigger";

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

  // API key (Bearer lsk_...) or browser session — agents can generate too.
  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

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

  // If stuck in generating_stories, check whether the latest round is stale
  // (>20 min old means the Modal worker was killed without updating the DB).
  if (project.status === "generating_stories") {
    const { data: latestRound } = await supabase
      .from("generation_rounds")
      .select("created_at")
      .eq("project_id", projectId)
      .order("round", { ascending: false })
      .limit(1)
      .maybeSingle();
    const stale =
      latestRound &&
      Date.now() - new Date(latestRound.created_at).getTime() > 20 * 60 * 1000;
    if (!stale) {
      return NextResponse.json(
        { error: `Cannot generate stories from status '${project.status}'` },
        { status: 409 },
      );
    }
    // Stale worker — treat as error so cleanup and retry proceed below
    await supabase
      .from("projects")
      .update({ status: "error", error_message: "Generation timed out — worker was killed. Please try again." })
      .eq("id", projectId);
    project.status = "error";
  }

  const allowedStatuses = ["transcribed", "stories_ready", "error"];
  if (!allowedStatuses.includes(project.status)) {
    return NextResponse.json(
      { error: `Cannot generate stories from status '${project.status}'` },
      { status: 409 },
    );
  }

  // Clean up orphaned placeholder stories and their generation_round rows
  // from the previous failed round so they don't corrupt conversation history.
  if (project.status === "error") {
    await supabase
      .from("stories")
      .delete()
      .eq("project_id", projectId)
      .eq("status", "generating");
    // Delete generation_round rows that now have no stories
    const { data: allRounds } = await supabase
      .from("generation_rounds")
      .select("id")
      .eq("project_id", projectId);
    const { data: roundsWithStories } = await supabase
      .from("stories")
      .select("generation_round_id")
      .eq("project_id", projectId)
      .not("generation_round_id", "is", null);
    const withStories = new Set((roundsWithStories ?? []).map((s) => s.generation_round_id));
    const orphanIds = (allRounds ?? []).map((r) => r.id).filter((id) => !withStories.has(id));
    if (orphanIds.length > 0) {
      await supabase.from("generation_rounds").delete().in("id", orphanIds);
    }
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

  triggerModal("generate", process.env.MODAL_GENERATE_URL, {
    project_id: projectId,
    round_id: round.id,
  });

  return NextResponse.json({ round_id: round.id }, { status: 202 });
}
