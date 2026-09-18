import { NextResponse } from "next/server";
import { createServiceClient } from "@/lib/supabase/service";

export const runtime = "nodejs";
// Never cache: a cached 200 would keep reporting healthy through an outage.
export const dynamic = "force-dynamic";

// How long to wait on Postgres before calling it down. A paused Supabase
// project doesn't refuse connections — its DNS record disappears and the
// connection hangs, so an unbounded query would stall the checker instead of
// failing it.
const DB_TIMEOUT_MS = 5_000;

// ── GET /api/health ────────────────────────────────────────────────────────
// Unauthenticated liveness probe for uptime monitoring. Deliberately touches
// Postgres: the frontend stays up on Vercel even when the database is gone
// (a paused Supabase project takes down auth and every API route while pages
// still render), so a check that only proves Next.js is serving would have
// reported healthy right through that outage.
//
// 200 {status:"ok"} when the database answers; 503 {status:"degraded"}
// otherwise — monitors alert on the status code alone.
//
// Doubles as the keep-warm target for .github/workflows/keep-warm.yml: the
// read below is the database activity that resets Supabase's free-tier idle
// timer.
export async function GET() {
  const startedAt = Date.now();

  try {
    const supabase = createServiceClient();

    // Cheapest possible round-trip that still proves Postgres answered:
    // head request, no rows, no count scan.
    const { error } = await supabase
      .from("projects")
      .select("id", { head: true })
      .limit(1)
      .abortSignal(AbortSignal.timeout(DB_TIMEOUT_MS));

    if (error) throw new Error(error.message);

    return NextResponse.json({
      status: "ok",
      database: "up",
      latency_ms: Date.now() - startedAt,
    });
  } catch (err) {
    const message = err instanceof Error ? err.message : String(err);
    return NextResponse.json(
      {
        status: "degraded",
        database: "down",
        // Bounded: surfaces the cause to a human reading the alert without
        // turning the response into a dump of internal state.
        error: message.slice(0, 200),
        latency_ms: Date.now() - startedAt,
      },
      { status: 503 },
    );
  }
}
