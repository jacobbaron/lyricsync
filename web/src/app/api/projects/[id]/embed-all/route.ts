import { after, NextResponse } from "next/server";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

// ── POST /api/projects/[id]/embed-all ───────────────────────────────────────
// SEARCH S4 (#121): library-wide embedding backfill for a project. Finds every
// "real" clip (uploaded — r2_key set) that lacks a pooled clip_embeddings row
// and doesn't already have an embedding run in flight, then triggers the same
// Modal embed_clip worker POST /api/clips/[id]/embed uses, for each — but
// sequenced (not fired concurrently), since this is real CPU compute cost
// across a whole library, not a single on-demand clip.
//
// Standalone MVP: this ticket predates #111 (Modal cron sweeper +
// attempts-counter retry infra) and #112 (perception-on-ingest), which don't
// exist yet. This is a manual lever, not folded into unified backfill
// machinery — fold it into #111 once that sweeper ships.
//
// API-key callable (Authorization: Bearer lsk_...).
// Response: { enqueued, skipped, already_done }
//   enqueued     — clips a new embedding run was just kicked off for
//   skipped      — clips that already have a run in flight (not re-triggered)
//   already_done — clips that already have a pooled embedding

function sleep(ms: number) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

// Space out Modal spawns so a big backfill doesn't slam the embed worker with
// dozens of concurrent containers at once.
const SPAWN_DELAY_MS = 400;

export async function POST(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: projectId } = await context.params;

  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data: project } = await supabase
    .from("projects")
    .select("id")
    .eq("id", projectId)
    .maybeSingle();
  if (!project) {
    return NextResponse.json({ error: "Project not found" }, { status: 404 });
  }

  // "Real" clips = ones with an uploaded video (mirrors the /embed route's
  // own r2_key guard) — nothing to embed for a clip that never finished
  // uploading.
  const { data: clips, error: clipsError } = await supabase
    .from("clips")
    .select("id, r2_key")
    .eq("project_id", projectId)
    .not("r2_key", "is", null);

  if (clipsError) {
    return NextResponse.json({ error: clipsError.message }, { status: 500 });
  }

  const clipIds = (clips ?? []).map((c) => c.id);
  if (clipIds.length === 0) {
    return NextResponse.json({ enqueued: 0, skipped: 0, already_done: 0 });
  }

  // Ground truth for "already embedded": a pooled (t IS NULL) clip_embeddings
  // row — that's the row semantic search actually reads recall from.
  const { data: pooledRows, error: pooledError } = await supabase
    .from("clip_embeddings")
    .select("clip_id")
    .in("clip_id", clipIds)
    .is("t", null);
  if (pooledError) {
    return NextResponse.json({ error: pooledError.message }, { status: 500 });
  }
  const doneSet = new Set((pooledRows ?? []).map((r) => r.clip_id as string));

  // Clips with an embedding run already in flight — don't double-enqueue.
  const { data: inFlightRows, error: inFlightError } = await supabase
    .from("clip_signals")
    .select("clip_id")
    .in("clip_id", clipIds)
    .eq("kind", "embedding")
    .eq("status", "processing");
  if (inFlightError) {
    return NextResponse.json({ error: inFlightError.message }, { status: 500 });
  }
  const inFlightSet = new Set(
    (inFlightRows ?? []).map((r) => r.clip_id as string),
  );

  const toEmbed = clipIds.filter(
    (id) => !doneSet.has(id) && !inFlightSet.has(id),
  );
  const alreadyDone = doneSet.size;
  const skipped = inFlightSet.size;

  if (toEmbed.length === 0) {
    return NextResponse.json({
      enqueued: 0,
      skipped,
      already_done: alreadyDone,
    });
  }

  const embedUrl =
    process.env.MODAL_EMBED_URL ??
    "https://jacobbaron--lyricsync-embed-clip.modal.run";
  const webhookSecret = process.env.MODAL_WEBHOOK_SECRET;

  // Guard signal creation on the secret being configured — a clip_signals row
  // with no corresponding Modal dispatch is a permanent false-positive for the
  // in-flight check above (nothing would ever flip it out of "processing"),
  // silently and permanently excluding that clip from every future backfill
  // call. Mirrors merge/route.ts, which only ever inserts a signal row inside
  // the `if (webhookSecret)` guard.
  if (!webhookSecret) {
    console.warn(
      "[embed-all] MODAL_WEBHOOK_SECRET not set — embeds not triggered",
    );
    return NextResponse.json({
      enqueued: 0,
      skipped,
      already_done: alreadyDone,
    });
  }

  // Create the clip_signals rows synchronously (cheap DB writes) so the
  // summary below is accurate even while the deferred Modal spawns are still
  // trickling out below.
  const signalIds: string[] = [];
  for (const clipId of toEmbed) {
    const { data: signal, error } = await supabase
      .from("clip_signals")
      .insert({ clip_id: clipId, kind: "embedding", status: "processing" })
      .select("id")
      .single();
    if (error || !signal) {
      console.error(
        `[embed-all] failed to create signal for clip ${clipId}:`,
        error?.message,
      );
      continue;
    }
    signalIds.push(signal.id);
  }

  if (signalIds.length > 0) {
    // One after() callback that walks the list sequentially — deliberately
    // NOT one after() per clip (that would fire every spawn concurrently,
    // exactly what we're avoiding for a library-wide backfill).
    after(async () => {
      for (const signalId of signalIds) {
        try {
          const res = await fetch(embedUrl, {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "x-webhook-secret": webhookSecret,
            },
            body: JSON.stringify({ signal_id: signalId }),
          });
          if (!res.ok) {
            const text = await res.text().catch(() => "");
            console.error(
              `[embed-all] trigger ${res.status} for ${signalId}: ${text}`,
            );
          }
        } catch (err) {
          console.error(`[embed-all] trigger failed for ${signalId}:`, err);
        }
        await sleep(SPAWN_DELAY_MS);
      }
    });
  }

  return NextResponse.json({
    enqueued: signalIds.length,
    skipped,
    already_done: alreadyDone,
  });
}
