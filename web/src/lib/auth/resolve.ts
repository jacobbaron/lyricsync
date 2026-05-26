import type { SupabaseClient } from "@supabase/supabase-js";
import { createClient } from "@/lib/supabase/server";
import { createServiceClient } from "@/lib/supabase/service";
import { createTokenClient } from "@/lib/supabase/token";
import { hashApiKey, isApiKey, mintUserJwt } from "./apiKey";

export interface ResolvedAuth {
  supabase: SupabaseClient;
  ownerEmail: string;
}

// Resolves the caller's identity from either an API key (Authorization: Bearer
// lsk_...) or a browser session cookie, and returns an RLS-scoped Supabase
// client plus the owner email. Returns null when the request is unauthenticated.
//
// Both paths yield a client whose queries are scoped to the owner by RLS, so
// route handlers don't need to filter by owner themselves.
export async function resolveAuth(
  request: Request,
): Promise<ResolvedAuth | null> {
  const header = request.headers.get("authorization") ?? "";
  const bearer = header.startsWith("Bearer ") ? header.slice(7).trim() : "";

  if (bearer && isApiKey(bearer)) {
    const service = createServiceClient();
    const { data: key } = await service
      .from("api_keys")
      .select("id, owner, owner_id, revoked_at")
      .eq("key_hash", hashApiKey(bearer))
      .maybeSingle();

    if (!key || key.revoked_at) return null;

    // Best-effort last-used timestamp; failure here must not block the request.
    void service
      .from("api_keys")
      .update({ last_used_at: new Date().toISOString() })
      .eq("id", key.id);

    const jwt = mintUserJwt({ email: key.owner, sub: key.owner_id });
    return { supabase: createTokenClient(jwt), ownerEmail: key.owner };
  }

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();
  if (!user?.email) return null;

  return { supabase, ownerEmail: user.email };
}
