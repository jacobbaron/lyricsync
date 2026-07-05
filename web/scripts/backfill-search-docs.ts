// SEARCH S2 (#119): one-off/backfill populate for `clip_search_docs`
// (transcript-window FTS documents).
//
// clips.visual_description and visual_analyses highlights are indexed via
// generated tsvector columns (see the S2 migration), which Postgres computes
// for *every existing row* the moment the column is added — no backfill
// script needed for those two sources. Transcript text lives in R2
// (projects/{id}/merged.json), which Postgres can't reach during a migration,
// so this script does the one-time (or re-runnable) population by reading R2
// directly and writing windowed transcript docs with the service role key
// (bypasses RLS — this touches every project, not just one caller's).
//
// The live/incremental path for *new* transcripts is
// web/src/app/api/projects/[id]/merge/route.ts, which calls the same
// windowTranscript() helper right after it (re)builds merged.json — this
// script exists only to catch projects that were merged *before* this
// migration shipped.
//
// Usage (run from web/, needs Node >=22 for --experimental-strip-types):
//   SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
//   R2_ENDPOINT=... CLOUDFLARE_R2_ACCESS_KEY_ID=... \
//   CLOUDFLARE_R2_SECRET_ACCESS_KEY=... R2_BUCKET_NAME=... \
//     node --experimental-strip-types scripts/backfill-search-docs.ts [--project <project_id>]
//
// (Same env vars Modal already uses via the `lyricsync-secrets` secret — see
// CLAUDE.md's "Key secrets" table.)

import { createClient } from "@supabase/supabase-js";
import { GetObjectCommand, S3Client } from "@aws-sdk/client-s3";
import {
  windowTranscript,
  type WindowableWord,
} from "../src/lib/search/transcriptWindows.ts";

const PAGE_SIZE = 1000;

interface ClipRow {
  id: string;
  project_id: string;
  filename: string | null;
}

interface MergedWord {
  text?: string;
  local_start?: number;
  source?: string;
}

function requireEnv(name: string): string {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

function parseArgs(argv: string[]): { projectId: string | null } {
  const idx = argv.indexOf("--project");
  return { projectId: idx >= 0 ? argv[idx + 1] ?? null : null };
}

async function fetchAllRows<T>(
  query: (
    from: number,
    to: number,
  ) => PromiseLike<{ data: unknown[] | null; error: { message: string } | null }>,
): Promise<T[]> {
  const rows: T[] = [];
  for (let from = 0; ; from += PAGE_SIZE) {
    const { data, error } = await query(from, from + PAGE_SIZE - 1);
    if (error) throw new Error(error.message);
    const batch = (data ?? []) as T[];
    rows.push(...batch);
    if (batch.length < PAGE_SIZE) break;
  }
  return rows;
}

async function getObjectText(s3: S3Client, bucket: string, key: string): Promise<string> {
  const res = await s3.send(new GetObjectCommand({ Bucket: bucket, Key: key }));
  if (!res.Body) throw new Error(`Empty body for R2 key: ${key}`);
  return res.Body.transformToString("utf-8");
}

async function main() {
  const { projectId: onlyProjectId } = parseArgs(process.argv.slice(2));

  const supabase = createClient(
    requireEnv("SUPABASE_URL"),
    requireEnv("SUPABASE_SERVICE_ROLE_KEY"),
    { auth: { persistSession: false } },
  );

  const bucket = requireEnv("R2_BUCKET_NAME");
  const s3 = new S3Client({
    region: "auto",
    endpoint: requireEnv("R2_ENDPOINT"),
    credentials: {
      accessKeyId: requireEnv("CLOUDFLARE_R2_ACCESS_KEY_ID"),
      secretAccessKey: requireEnv("CLOUDFLARE_R2_SECRET_ACCESS_KEY"),
    },
  });

  const clips = await fetchAllRows<ClipRow>((from, to) => {
    let q = supabase.from("clips").select("id, project_id, filename").range(from, to);
    if (onlyProjectId) q = q.eq("project_id", onlyProjectId);
    return q;
  });

  const projectIds = Array.from(new Set(clips.map((c) => c.project_id)));
  console.log(`Found ${clips.length} clips across ${projectIds.length} project(s).`);

  let clipsIndexed = 0;
  let windowsWritten = 0;
  let projectsSkipped = 0;

  for (const projectId of projectIds) {
    const projectClips = clips.filter((c) => c.project_id === projectId);
    const clipByFilename = new Map<string, ClipRow>();
    for (const c of projectClips) {
      if (c.filename) clipByFilename.set(c.filename, c);
    }

    let merged: { words?: MergedWord[] };
    try {
      const text = await getObjectText(s3, bucket, `projects/${projectId}/merged.json`);
      merged = JSON.parse(text) as { words?: MergedWord[] };
    } catch (err) {
      console.log(`  [skip] project ${projectId}: no merged.json yet (${(err as Error).message})`);
      projectsSkipped += 1;
      continue;
    }

    const wordsByClip = new Map<string, WindowableWord[]>();
    for (const w of merged.words ?? []) {
      if (!w.source || w.local_start == null || !w.text) continue;
      const clip = clipByFilename.get(w.source);
      if (!clip) continue;
      const arr = wordsByClip.get(clip.id);
      const word = { text: w.text, local_start: w.local_start };
      if (arr) arr.push(word);
      else wordsByClip.set(clip.id, [word]);
    }

    let projectWindows = 0;
    for (const [clipId, words] of wordsByClip) {
      const windows = windowTranscript(words);

      const { error: delError } = await supabase
        .from("clip_search_docs")
        .delete()
        .eq("clip_id", clipId)
        .eq("kind", "transcript");
      if (delError) {
        console.error(`  [error] clip ${clipId}: delete failed: ${delError.message}`);
        continue;
      }

      if (windows.length > 0) {
        const { error: insError } = await supabase.from("clip_search_docs").insert(
          windows.map((w) => ({
            clip_id: clipId,
            project_id: projectId,
            kind: "transcript",
            window_idx: w.window_idx,
            source_text: w.source_text,
            timestamp: w.timestamp,
          })),
        );
        if (insError) {
          console.error(`  [error] clip ${clipId}: insert failed: ${insError.message}`);
          continue;
        }
      }

      clipsIndexed += 1;
      windowsWritten += windows.length;
      projectWindows += windows.length;
    }

    console.log(
      `  project ${projectId}: ${wordsByClip.size} clip(s) with transcript, ${projectWindows} window(s)`,
    );
  }

  console.log(
    `Done. ${clipsIndexed} clip(s) indexed, ${windowsWritten} window(s) written, ` +
      `${projectsSkipped} project(s) skipped (no transcript yet).`,
  );
}

main().catch((err) => {
  console.error("backfill-search-docs failed:", err);
  process.exit(1);
});
