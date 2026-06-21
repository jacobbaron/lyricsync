import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { headObjectSize } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── GET /api/projects/[id]/storage ─────────────────────────────────────────
// Reports R2 storage usage for a single project, broken down by category, so
// the user can see what's taking up space and clean it up. Sizes are read
// live from R2 via HeadObject (no sizes are persisted in the DB), so the
// numbers reflect what's actually stored right now.
//
// Response:
//   {
//     project_id,
//     total_bytes,
//     categories: {
//       clips:       { bytes, count },   // source video uploads
//       renders:     { bytes, count },   // rendered cut outputs
//       transcripts: { bytes, count },   // per-clip transcript JSON
//       analyses:    { bytes, count },   // visual-analysis result JSON
//     },
//     renders: [ { story_id, title, bytes, created_at } ],  // for cleanup UI
//   }

interface Category {
  bytes: number;
  count: number;
}

function emptyCategory(): Category {
  return { bytes: 0, count: 0 };
}

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

  // Confirm the project exists and is owned by the caller (RLS scopes the row).
  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Gather every R2 key associated with the project.
  const { data: clips } = await supabase
    .from("clips")
    .select("id, r2_key, transcript_r2_key")
    .eq("project_id", projectId);

  const { data: stories } = await supabase
    .from("stories")
    .select("id, title, render_r2_key, created_at")
    .eq("project_id", projectId);

  const clipIds = (clips ?? []).map((c) => c.id);
  let analyses: Array<{ result_r2_key: string | null }> = [];
  if (clipIds.length > 0) {
    const { data } = await supabase
      .from("visual_analyses")
      .select("result_r2_key")
      .in("clip_id", clipIds);
    analyses = data ?? [];
  }

  // Resolve every key's size in parallel. Each entry carries its category so
  // we can total without depending on lookup order.
  type Job = {
    key: string;
    category: "clips" | "renders" | "transcripts" | "analyses";
    storyId?: string;
    title?: string | null;
    createdAt?: string;
  };
  const jobs: Job[] = [];
  for (const c of clips ?? []) {
    if (c.r2_key) jobs.push({ key: c.r2_key, category: "clips" });
    if (c.transcript_r2_key)
      jobs.push({ key: c.transcript_r2_key, category: "transcripts" });
  }
  for (const s of stories ?? []) {
    if (s.render_r2_key)
      jobs.push({
        key: s.render_r2_key,
        category: "renders",
        storyId: s.id,
        title: s.title,
        createdAt: s.created_at,
      });
  }
  for (const a of analyses) {
    if (a.result_r2_key)
      jobs.push({ key: a.result_r2_key, category: "analyses" });
  }

  const sizes = await Promise.all(jobs.map((j) => headObjectSize(j.key)));

  const categories: Record<Job["category"], Category> = {
    clips: emptyCategory(),
    renders: emptyCategory(),
    transcripts: emptyCategory(),
    analyses: emptyCategory(),
  };
  const renders: Array<{
    story_id: string;
    title: string | null;
    bytes: number;
    created_at: string | null;
  }> = [];
  let total = 0;

  jobs.forEach((job, i) => {
    const bytes = sizes[i];
    if (bytes == null) return; // object missing — skip
    categories[job.category].bytes += bytes;
    categories[job.category].count += 1;
    total += bytes;
    if (job.category === "renders" && job.storyId) {
      renders.push({
        story_id: job.storyId,
        title: job.title ?? null,
        bytes,
        created_at: job.createdAt ?? null,
      });
    }
  });

  renders.sort((a, b) => b.bytes - a.bytes);

  return NextResponse.json({
    project_id: projectId,
    total_bytes: total,
    categories,
    renders,
  });
}
