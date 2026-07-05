import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";
import { getObjectText, putObjectJson } from "@/lib/r2/client";
import { windowTranscript } from "@/lib/search/transcriptWindows";

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
  recorded_at: string | null; // ISO instant the source clip started recording
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
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  // API key (Bearer lsk_...) or browser session — agents drive this too.
  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

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
    .select("id, filename, r2_key, transcript_r2_key, status, recorded_at, created_at")
    .eq("project_id", projectId);

  if (clipsError) {
    return NextResponse.json({ error: clipsError.message }, { status: 500 });
  }

  // Include both freshly transcribed clips and already-aligned ones. Re-merging
  // over the full set (not just new clips) lets the user add videos to a project
  // whose transcription already finished and rebuild the whole transcript.
  const transcribedClips = (clips ?? []).filter(
    (c) =>
      c.transcript_r2_key &&
      (c.status === "transcribed_raw" || c.status === "aligned"),
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
      // Wall-clock anchor for the clip: real recording time when known,
      // otherwise the upload time so the UI can always show a date/time.
      const recordedAt = clip.recorded_at ?? clip.created_at ?? null;
      for (const w of words) {
        allWords.push({
          text: w.word,
          global_start: w.start, // global_start = local_start (no offset)
          global_end: w.end,
          local_start: w.start,
          local_end: w.end,
          source: clip.filename ?? "",
          source_path: clip.r2_key ?? "",
          recorded_at: recordedAt,
        });
      }
    }

    allWords.sort((a, b) => a.global_start - b.global_start);

    // Upload merged.json
    const mergedKey = `projects/${projectId}/merged.json`;
    await putObjectJson(mergedKey, { words: allWords });

    // SEARCH S2 (#119): refresh the FTS transcript-window docs for every
    // merged clip, so newly-built/rebuilt transcripts are searchable via
    // /api/search without a manual reindex (clips.visual_description and
    // visual_analyses highlights index themselves via generated tsvector
    // columns — this is the one source that lives in R2, so it needs an
    // explicit hook here). Best-effort per clip: one clip's failure doesn't
    // block the merge or the other clips' refresh.
    for (const { clip, words } of wordLists) {
      try {
        const windows = windowTranscript(
          words.map((w) => ({ text: w.word, local_start: w.start })),
        );
        const { error: delError } = await supabase
          .from("clip_search_docs")
          .delete()
          .eq("clip_id", clip.id)
          .eq("kind", "transcript");
        if (delError) {
          console.error(
            `[merge] search-doc delete failed for clip ${clip.id} (non-fatal):`,
            delError.message,
          );
          continue;
        }
        if (windows.length > 0) {
          const { error: docsError } = await supabase
            .from("clip_search_docs")
            .insert(
              windows.map((w) => ({
                clip_id: clip.id,
                project_id: projectId,
                kind: "transcript",
                window_idx: w.window_idx,
                source_text: w.source_text,
                timestamp: w.timestamp,
              })),
            );
          if (docsError) {
            console.error(
              `[merge] search-doc insert failed for clip ${clip.id} (non-fatal):`,
              docsError.message,
            );
          }
        }
      } catch (err) {
        console.error(
          `[merge] search-doc refresh failed for clip ${clip.id} (non-fatal):`,
          err,
        );
      }
    }

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

    // Kick off the canonical visual analysis for any clip that lacks one
    // (roadmap 1.1). The UI pipeline merges here on Vercel and never touches
    // Modal's align worker, so this is where the perception chain must start.
    // Best-effort: a failure here must not fail the merge.
    const CANONICAL_VARIANT = "v2";
    const analyzeUrl =
      process.env.MODAL_ANALYZE_URL ??
      "https://jacobbaron--lyricsync-analyze-visuals.modal.run";
    const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;
    if (webhookSecret) {
      for (const clip of transcribedClips) {
        try {
          const { data: existing } = await supabase
            .from("visual_analyses")
            .select("id")
            .eq("clip_id", clip.id)
            .eq("variant", CANONICAL_VARIANT)
            .in("status", ["analyzing", "done"])
            .limit(1);
          if (existing && existing.length > 0) continue;

          const { data: analysis } = await supabase
            .from("visual_analyses")
            .insert({
              clip_id: clip.id,
              variant: CANONICAL_VARIANT,
              status: "analyzing",
            })
            .select("id")
            .single();
          if (!analysis) continue;

          const analysisId = analysis.id;
          after(async () => {
            try {
              const res = await fetch(analyzeUrl, {
                method: "POST",
                headers: {
                  "Content-Type": "application/json",
                  "x-webhook-secret": webhookSecret,
                },
                body: JSON.stringify({ analysis_id: analysisId }),
              });
              if (!res.ok) {
                const text = await res.text().catch(() => "");
                console.error(`[merge] analyze trigger ${res.status}: ${text}`);
              }
            } catch (err) {
              console.error("[merge] analyze trigger failed:", err);
            }
          });
        } catch (err) {
          console.error("[merge] visual analysis spawn failed (non-fatal):", err);
        }
      }
    }

    // Kick off the clip audio analysis (waveform + Silero VAD curve) for each
    // clip, so the VAD-fused clean_speech cut works without a manual "Analyze
    // audio" step. Best-effort + idempotent (the worker overwrites its R2
    // outputs); a failure here must not fail the merge.
    const audioAnalyzeUrl =
      process.env.MODAL_ANALYZE_AUDIO_URL ??
      analyzeUrl.replace("analyze-visuals", "analyze-clip-audio");
    if (webhookSecret) {
      for (const clip of transcribedClips) {
        after(async () => {
          try {
            const res = await fetch(audioAnalyzeUrl, {
              method: "POST",
              headers: {
                "Content-Type": "application/json",
                "x-webhook-secret": webhookSecret,
              },
              body: JSON.stringify({ clip_id: clip.id }),
            });
            if (!res.ok) {
              const text = await res.text().catch(() => "");
              console.error(`[merge] audio analyze trigger ${res.status}: ${text}`);
            }
          } catch (err) {
            console.error("[merge] audio analyze trigger failed:", err);
          }
        });
      }
    }

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
