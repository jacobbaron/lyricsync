import { createClient as createSupabaseClient } from "@supabase/supabase-js";

// Service-role client: bypasses Row Level Security. Use only on the server for
// operations that must run before a user identity is known — e.g. looking up an
// API key by its hash during authentication. Never expose this client or its
// key to the browser.
export function createServiceClient() {
  const supabaseUrl = process.env.SUPABASE_URL;
  const serviceRoleKey = process.env.SUPABASE_SERVICE_ROLE_KEY;

  if (!supabaseUrl || !serviceRoleKey) {
    const missing = [
      !supabaseUrl && "SUPABASE_URL",
      !serviceRoleKey && "SUPABASE_SERVICE_ROLE_KEY",
    ]
      .filter(Boolean)
      .join(", ");
    throw new Error(
      `Supabase service client misconfigured: ${missing} not set.`,
    );
  }

  return createSupabaseClient(supabaseUrl, serviceRoleKey, {
    auth: { persistSession: false, autoRefreshToken: false },
  });
}
