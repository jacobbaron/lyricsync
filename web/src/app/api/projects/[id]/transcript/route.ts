import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { getObjectText } from "@/lib/r2/client";

export const runtime = "nodejs";

export async function GET(
  _request: Request,
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

  // Verify project exists and belongs to user (RLS enforces ownership).
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }
  if (project.status !== "transcribed" && project.status !== "done") {
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
