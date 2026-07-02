import { NextResponse } from "next/server";
import type { SupabaseClient } from "@supabase/supabase-js";

/**
 * Inserts a clip_signals row and returns the new row's id, or a 500
 * NextResponse on failure.
 */
export async function createClipSignal(
  supabase: SupabaseClient,
  clipId: string,
  kind: string,
): Promise<{ id: string } | NextResponse> {
  const { data: signal, error } = await supabase
    .from("clip_signals")
    .insert({ clip_id: clipId, kind, status: "processing" })
    .select("id")
    .single();

  if (error || !signal) {
    return NextResponse.json(
      { error: error?.message ?? "Failed to create signal" },
      { status: 500 },
    );
  }

  return signal;
}
