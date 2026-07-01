import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";
import {
  songObjectKey,
  presignClipUpload,
  UPLOAD_URL_TTL_SECONDS,
} from "@/lib/r2/client";

export const runtime = "nodejs";

// ── POST /api/projects/[id]/songs ───────────────────────────────────────────
// Register a finished-mix song for a project and hand back a presigned PUT URL
// to upload the audio to R2. Mirrors the clip upload flow. The client PUTs the
// file, then calls POST /api/songs/[id]/complete to mark it ready.

const Body = z.object({
  filename: z.string().trim().min(1).max(500),
  contentType: z.string().trim().min(1).max(200),
});

function extensionFromFilename(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0 || dot === filename.length - 1) return "mp3";
  return filename.slice(dot + 1).toLowerCase().replace(/[^a-z0-9]/g, "") || "mp3";
}

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const body = await request.json().catch(() => null);
  const parsed = Body.safeParse(body);
  if (!parsed.success) {
    return NextResponse.json(
      { error: "Invalid request body", issues: parsed.error.issues },
      { status: 400 },
    );
  }

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  // RLS scopes this to the caller's projects.
  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  const { data: song, error: insertError } = await supabase
    .from("songs")
    .insert({ project_id: projectId, filename: parsed.data.filename })
    .select("id")
    .single();
  if (insertError || !song) {
    return NextResponse.json(
      { error: insertError?.message ?? "Failed to create song" },
      { status: 500 },
    );
  }

  const key = songObjectKey(
    projectId,
    song.id,
    extensionFromFilename(parsed.data.filename),
  );

  let uploadUrl: string;
  try {
    uploadUrl = await presignClipUpload(key, parsed.data.contentType);
  } catch (err) {
    // Roll back so we don't leak an orphan row on R2 misconfig.
    await supabase.from("songs").delete().eq("id", song.id);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to presign upload" },
      { status: 500 },
    );
  }

  await supabase.from("songs").update({ r2_key: key }).eq("id", song.id);

  return NextResponse.json(
    {
      songId: song.id,
      uploadUrl,
      r2Key: key,
      expiresInSeconds: UPLOAD_URL_TTL_SECONDS,
    },
    { status: 201 },
  );
}
