import { NextResponse } from "next/server";
import { z } from "zod";
import { createClient } from "@/lib/supabase/server";

export const runtime = "nodejs";

const PatchBody = z.object({
  status: z.literal("uploading_complete"),
});

export async function PATCH(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const body = await request.json().catch(() => null);
  const parsed = PatchBody.safeParse(body);
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

  const { data, error } = await supabase
    .from("clips")
    .update({ status: parsed.data.status })
    .eq("id", clipId)
    .select("id, status")
    .maybeSingle();

  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }
  if (!data) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }

  return NextResponse.json(data);
}

// DELETE — removes a clip row that is stuck in uploading or error state.
// Only clips that haven't been transcribed yet can be deleted; once a clip
// has been aligned its data is part of the merged transcript.
const DELETABLE_STATUSES = ["uploading", "uploading_complete", "error"];

export async function DELETE(
  _request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: clipId } = await context.params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) {
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  // Fetch first to check status (RLS ensures ownership via projects join)
  const { data: clip } = await supabase
    .from("clips")
    .select("id, status")
    .eq("id", clipId)
    .maybeSingle();

  if (!clip) {
    return NextResponse.json({ error: "Clip not found" }, { status: 404 });
  }
  if (!DELETABLE_STATUSES.includes(clip.status)) {
    return NextResponse.json(
      { error: `Cannot delete clip with status '${clip.status}'` },
      { status: 409 },
    );
  }

  const { error } = await supabase.from("clips").delete().eq("id", clipId);
  if (error) {
    return NextResponse.json({ error: error.message }, { status: 500 });
  }

  return new NextResponse(null, { status: 204 });
}
