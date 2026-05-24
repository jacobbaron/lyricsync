import { NextResponse } from "next/server";
import { z } from "zod";
import { createClient } from "@/lib/supabase/server";
import {
  clipObjectKey,
  presignClipUpload,
  UPLOAD_URL_TTL_SECONDS,
} from "@/lib/r2/client";

export const runtime = "nodejs";

const Body = z.object({
  filename: z.string().trim().min(1).max(500),
  contentType: z.string().trim().min(1).max(200),
});

function extensionFromFilename(filename: string): string {
  const dot = filename.lastIndexOf(".");
  if (dot < 0 || dot === filename.length - 1) return "bin";
  return filename.slice(dot + 1).toLowerCase().replace(/[^a-z0-9]/g, "") || "bin";
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

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // RLS scopes this query to projects owned by the user, so a missing row
  // here means either the project doesn't exist or doesn't belong to them.
  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  const { data: clip, error: insertError } = await supabase
    .from("clips")
    .insert({ project_id: projectId, filename: parsed.data.filename })
    .select("id")
    .single();
  if (insertError || !clip) {
    return NextResponse.json(
      { error: insertError?.message ?? "Failed to create clip" },
      { status: 500 },
    );
  }

  const key = clipObjectKey(
    projectId,
    clip.id,
    extensionFromFilename(parsed.data.filename),
  );

  let uploadUrl: string;
  try {
    uploadUrl = await presignClipUpload(key, parsed.data.contentType);
  } catch (err) {
    // Roll back the clip row so we don't leak orphan rows on R2 misconfig.
    await supabase.from("clips").delete().eq("id", clip.id);
    return NextResponse.json(
      { error: err instanceof Error ? err.message : "Failed to presign upload" },
      { status: 500 },
    );
  }

  await supabase.from("clips").update({ r2_key: key }).eq("id", clip.id);

  return NextResponse.json(
    {
      clipId: clip.id,
      uploadUrl,
      r2Key: key,
      expiresInSeconds: UPLOAD_URL_TTL_SECONDS,
    },
    { status: 201 },
  );
}
