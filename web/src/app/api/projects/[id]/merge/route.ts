import { NextResponse } from "next/server";
import { createClient } from "@/lib/supabase/server";
import { getObjectText, putObjectJson } from "@/lib/r2/client";

export const runtime = "nodejs";

// ── types ─────────────────────────────────────────────────────────────────

interface RawWord {
  word?: string;
  text?: string;
  start?: number;
  end?: number;
}

interface RawSegment {
  words?: RawWord[];
}

interface RawTranscript {
  words?: RawWord[];
  segments?: RawSegment[];
}

interface MergedWord {
  text: string;
  global_start: number;
  global_end: number;
  local_start: number;
  local_end: number;
  source: string;         // clip filename — matches ranges_json source field
  source_path: string;    // R2 key of the original video
}

// ── helpers ───────────────────────────────────────────────────────────────

/**
 * Normalise a raw Whisper transcript to a flat word list.
 * Mirrors the Python _words_from() in modal/app.py.
 */
function wordsFrom(data: RawTranscript): Array<{ word: string; start: number; end: number }> {
  const topLevel: RawWord[] = data.words ?? [];

  // Fall back to flattening per-segment word lists
  const words: RawWord[] = topLevel.length > 0
    ? topLevel
    : (data.segments ?? []).flatMap((seg) => seg.words ?? []);

  return words
    .filter((w) => w.start != null && w.end != null)
    .map((w) => ({
      word: (w.word ?? w.text ?? "").trim(),
      start: Number(w.start),
      end: Number(w.end),
    }));
}

// ── POST /api/projects/[id]/merge ─────────────────────────────────────────
//
// Builds merged.json from raw Whisper transcripts — no ML required.
// Called automatically by StatusPoller once all clips reach transcribed_raw.
//
// NOTE: global_start is set to 0 for all clips. Multi-camera timestamp sync
// requires the WhisperX alignment step (POST /api/projects/[id]/align), which
// is preserved but not wired into the automatic pipeline. Re-enable it by
// changing StatusPoller to call /align instead of /merge when needed.

export async function POST(
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

  // Verify project ownership (RLS enforces this too)
  const { data: project } = await supabase
    .from("projects")
    .select("id, status")
    .eq("id", projectId)
    .maybeSingle();

  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // Fetch all clips that have a transcript
  const { data: clips, error: clipsError } = await supabase
    .from("clips")
    .select("id, filename, r2_key, transcript_r2_key, status")
    .eq("project_id", projectId);

  if (clipsError) {
    return NextResponse.json({ error: clipsError.message }, { status: 500 });
  }

  const transcribedClips = (clips ?? []).filter(
    (c) => c.transcript_r2_key && c.status === "transcribed_raw",
  );

  if (transcribedClips.length === 0) {
    return NextResponse.json(
      { error: "No transcribed clips found" },
      { status: 409 },
    );
  }

  try {
    // Fetch all transcripts in parallel and build the merged word list.
    // global_start is 0 for all clips — adequate for single-camera recordings.
    // For multi-camera sync, re-enable the /align endpoint (WhisperX + ffprobe).
    const wordLists = await Promise.all(
      transcribedClips.map(async (clip) => {
        const raw = await getObjectText(clip.transcript_r2_key!);
        const transcript = JSON.parse(raw) as RawTranscript;
        const words = wordsFrom(transcript);
        return { clip, words };
      }),
    );

    const allWords: MergedWord[] = [];
    for (const { clip, words } of wordLists) {
      for (const w of words) {
        allWords.push({
          text: w.word,
          global_start: w.start, // global_start = local_start (no offset)
          global_end: w.end,
          local_start: w.start,
          local_end: w.end,
          source: clip.filename ?? "",
          source_path: clip.r2_key ?? "",
        });
      }
    }

    allWords.sort((a, b) => a.global_start - b.global_start);

    // Upload merged.json
    const mergedKey = `projects/${projectId}/merged.json`;
    await putObjectJson(mergedKey, { words: allWords });

    // Mark all transcribed clips as aligned and project as transcribed
    await supabase
      .from("clips")
      .update({ status: "aligned", global_start: 0 })
      .eq("project_id", projectId)
      .eq("status", "transcribed_raw");

    await supabase
      .from("projects")
      .update({ status: "transcribed" })
      .eq("id", projectId);

    return NextResponse.json({
      words: allWords.length,
      clips: transcribedClips.length,
    });
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("[merge] error:", msg);
    await supabase
      .from("projects")
      .update({ status: "error", error_message: msg.slice(0, 500) })
      .eq("id", projectId);
    return NextResponse.json({ error: msg }, { status: 500 });
  }
}
