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

// ── GET /api/projects/[id]/songs ────────────────────────────────────────────
// List the project's songs, newest first, each with its per-clip sync state:
// the latest clip_alignments row per clip, with `offset` = song time at clip
// t=0 (song_t = clip_t + offset). `drift_ms` > DRIFT_WARN_MS means the footage
// is a different take than the master, so lip-sync will wander.

const DRIFT_WARN_MS = 40; // matches modal/song_sync.py DRIFT_WARN_MS

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

  const { data: songs, error } = await supabase
    .from("songs")
    .select("id, filename, duration_secs, status, created_at")
    .eq("project_id", projectId)
    .order("created_at", { ascending: false });
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  const songIds = (songs ?? []).map((s) => s.id);

  const { data: clips } = await supabase
    .from("clips")
    .select("id, filename")
    .eq("project_id", projectId);
  const clipName = new Map((clips ?? []).map((c) => [c.id, c.filename]));

  const { data: aligns } = songIds.length
    ? await supabase
        .from("clip_alignments")
        .select(
          "id, clip_id, song_id, footage_start, footage_end, song_start, cost, method, confidence, drift_ms, status, error, created_at",
        )
        .in("song_id", songIds)
        .order("created_at", { ascending: false })
    : { data: [] };

  // Latest alignment per (song, clip).
  const latest = new Map<string, NonNullable<typeof aligns>[number]>();
  for (const a of aligns ?? []) {
    const k = `${a.song_id}:${a.clip_id}`;
    if (!latest.has(k)) latest.set(k, a);
  }

  const out = (songs ?? []).map((s) => ({
    ...s,
    clips: [...latest.values()]
      .filter((a) => a.song_id === s.id)
      .map((a) => ({
        clip_id: a.clip_id,
        filename: clipName.get(a.clip_id) ?? null,
        alignment_id: a.id,
        status: a.status,
        offset:
          a.song_start == null
            ? null
            : Number((Number(a.song_start) - Number(a.footage_start)).toFixed(4)),
        footage_start: a.footage_start,
        footage_end: a.footage_end,
        method: a.method,
        confidence: a.confidence,
        cost: a.cost,
        drift_ms: a.drift_ms,
        drift_warning: a.drift_ms != null && Number(a.drift_ms) > DRIFT_WARN_MS,
        error: a.error,
      })),
  }));

  return NextResponse.json({ songs: out });
}
