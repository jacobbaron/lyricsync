import { NextResponse } from "next/server";
import { z } from "zod";
import { resolveAuth } from "@/lib/auth/resolve";

export const runtime = "nodejs";

export async function GET(request: Request) {
  const auth = await resolveAuth(request);
  if (!auth) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }
  const { supabase } = auth;

  const { data, error } = await supabase
    .from("projects")
    .select("id, name, status, created_at")
    .order("created_at", { ascending: false });

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return NextResponse.json(data ?? []);
}

const Body = z.object({
  name: z.string().trim().min(1).max(200),
});

export async function POST(request: Request) {
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
  const { supabase, ownerEmail } = auth;

  const { data, error } = await supabase
    .from("projects")
    .insert({ name: parsed.data.name, owner: ownerEmail })
    .select("id, name, status, created_at")
    .single();

  if (error || !data) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create project" },
      { status: 500 },
    );
  }

  return NextResponse.json(data, { status: 201 });
}
