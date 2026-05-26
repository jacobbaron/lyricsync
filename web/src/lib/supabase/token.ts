import { createClient as createSupabaseClient } from "@supabase/supabase-js";

// Builds a Supabase client authenticated by a bearer access token instead of a
// session cookie. The token carries the user's email claim, so RLS scopes data
// to that user exactly as it does for a browser session.
export function createTokenClient(accessToken: string) {
  const supabaseUrl = process.env.SUPABASE_URL;
  const supabaseAnonKey = process.env.SUPABASE_ANON_KEY;

  if (!supabaseUrl || !supabaseAnonKey) {
    const missing = [
      !supabaseUrl && "SUPABASE_URL",
      !supabaseAnonKey && "SUPABASE_ANON_KEY",
    ]
      .filter(Boolean)
      .join(", ");
    throw new Error(`Supabase token client misconfigured: ${missing} not set.`);
  }

  return createSupabaseClient(supabaseUrl, supabaseAnonKey, {
    auth: { persistSession: false, autoRefreshToken: false },
    global: { headers: { Authorization: `Bearer ${accessToken}` } },
  });
}
