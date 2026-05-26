import { NextResponse } from "next/server";
import { z } from "zod";
import { createClient } from "@/lib/supabase/server";
import { generateApiKey } from "@/lib/auth/apiKey";

export const runtime = "nodejs";

// ── GET /api/keys ──────────────────────────────────────────────────────────
// Lists the caller's active (non-revoked) API keys. Secrets are never returned;
// only label, display prefix, and timestamps.
export async function GET() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  const { data, error } = await supabase
    .from("api_keys")
    .select("id, label, key_prefix, created_at, last_used_at")
    .is("revoked_at", null)
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  return NextResponse.json(data ?? []);
}

const Body = z.object({ label: z.string().trim().max(100).optional() });

// ── POST /api/keys ─────────────────────────────────────────────────────────
// Issues a new API key owned by the caller. Returns the plaintext secret EXACTLY
// ONCE in the `key` field — it is not stored and cannot be retrieved again.
export async function POST(request: Request) {
  const body = await request.json().catch(() => ({}));
  const parsed = Body.safeParse(body ?? {});
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

  const { plaintext, hash, prefix } = generateApiKey();
  const { data, error } = await supabase
    .from("api_keys")
    .insert({
      owner: user.email,
      owner_id: user.id,
      key_hash: hash,
      key_prefix: prefix,
      label: parsed.data.label ?? null,
    })
    .select("id, label, key_prefix, created_at")
    .single();

  if (error || !data) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create key" },
      { status: 500 },
    );
  }

  return NextResponse.json({ ...data, key: plaintext }, { status: 201 });
}
