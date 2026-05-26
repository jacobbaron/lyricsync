import { NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { getObjectText } from "@/lib/r2/client";

export const runtime = "nodejs";

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

  // Verify project exists and belongs to user (RLS enforces ownership).
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }
  const TRANSCRIPT_READY = ["transcribed", "generating_stories", "stories_ready", "rendering", "done"];
  if (!TRANSCRIPT_READY.includes(project.status)) {
    return NextResponse.json(
      { error: "Transcript not yet available", status: project.status },
      { status: 409 },
    );
  }

  const key = `projects/${projectId}/merged.json`;
  try {
    const text = await getObjectText(key);
    const merged = JSON.parse(text);
    return NextResponse.json(merged);
  } catch (err) {
    console.error(`Failed to fetch ${key}:`, err);
    return NextResponse.json(
      { error: "Transcript file not found in storage" },
      { status: 404 },
    );
  }
}
