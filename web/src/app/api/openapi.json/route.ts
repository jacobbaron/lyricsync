import { NextResponse } from "next/server";
import { buildOpenApiDocument } from "@/lib/openapi/document";

export const runtime = "nodejs";

// ── GET /api/openapi.json ──────────────────────────────────────────────────
// The OpenAPI 3.1 document for the LyricSync API, generated from the Zod
// schemas (single source of truth). Public — the spec describes the auth, it
// doesn't expose data. Cached briefly; it only changes on deploy.

export async function GET() {
  const doc = buildOpenApiDocument();
  return NextResponse.json(doc, {
    headers: { "Cache-Control": "public, max-age=300" },
  });
}
