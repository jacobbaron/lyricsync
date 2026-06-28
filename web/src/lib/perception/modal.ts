import type { SupabaseClient } from "@supabase/supabase-js";

// Shared plumbing for the §1.3 interactive perception routes:
//   - resolve the Modal endpoint URL for a perception tool,
//   - call Modal synchronously (the caller wants results inline, unlike the
//     fire-and-forget analyze worker),
//   - read/write the clip_inspections cache.
//
// Each tool's Modal function is webhook-secret authenticated, exactly like the
// existing analyze_visuals endpoint.

export type PerceptionKind = "frames" | "describe" | "contact_sheet";

// Default deployed Modal URLs (override per-tool with the matching env var).
const DEFAULT_URLS: Record<PerceptionKind, string> = {
  frames: "https://jacobbaron--lyricsync-perception-frames.modal.run",
  describe: "https://jacobbaron--lyricsync-perception-describe.modal.run",
  contact_sheet:
    "https://jacobbaron--lyricsync-perception-contact-sheet.modal.run",
};

const ENV_KEYS: Record<PerceptionKind, string> = {
  frames: "MODAL_PERCEPTION_FRAMES_URL",
  describe: "MODAL_PERCEPTION_DESCRIBE_URL",
  contact_sheet: "MODAL_PERCEPTION_CONTACT_SHEET_URL",
};

export function perceptionUrl(kind: PerceptionKind): string {
  return process.env[ENV_KEYS[kind]] ?? DEFAULT_URLS[kind];
}

export class PerceptionError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

/** Call a Modal perception endpoint and return its parsed JSON.
 *
 * Throws PerceptionError(status, detail) on a non-2xx so the route can relay
 * the upstream status (404 clip / 409 no-video / 5xx ffmpeg) to the caller. */
export async function callPerception<T>(
  kind: PerceptionKind,
  body: Record<string, unknown>,
): Promise<T> {
  const secret = process.env.MODAL_WEBHOOK_SECRET;
  if (!secret) {
    throw new PerceptionError(500, "MODAL_WEBHOOK_SECRET not configured");
  }
  const res = await fetch(perceptionUrl(kind), {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-webhook-secret": secret,
    },
    body: JSON.stringify(body),
  });
  const text = await res.text().catch(() => "");
  if (!res.ok) {
    // Modal/FastAPI errors arrive as {"detail": "..."}.
    let detail = text;
    try {
      detail = JSON.parse(text)?.detail ?? text;
    } catch {
      /* keep raw text */
    }
    throw new PerceptionError(res.status, detail || "Modal request failed");
  }
  return (text ? JSON.parse(text) : {}) as T;
}

/** Build a stable cache key from (clipId, kind, params). Keys are sorted so
 * param ordering doesn't matter. */
export function cacheKey(
  clipId: string,
  kind: PerceptionKind,
  params: Record<string, unknown>,
): string {
  const ordered = Object.keys(params)
    .sort()
    .map((k) => `${k}=${JSON.stringify(params[k])}`)
    .join("&");
  return `${clipId}:${kind}:${ordered}`;
}

export async function readCache(
  supabase: SupabaseClient,
  key: string,
): Promise<Record<string, unknown> | null> {
  const { data } = await supabase
    .from("clip_inspections")
    .select("result")
    .eq("cache_key", key)
    .maybeSingle();
  return (data?.result as Record<string, unknown> | undefined) ?? null;
}

export async function writeCache(
  supabase: SupabaseClient,
  clipId: string,
  kind: PerceptionKind,
  key: string,
  params: Record<string, unknown>,
  result: Record<string, unknown>,
): Promise<void> {
  // Upsert on the unique cache_key so concurrent identical calls coalesce.
  await supabase
    .from("clip_inspections")
    .upsert(
      { clip_id: clipId, kind, cache_key: key, params, result },
      { onConflict: "cache_key" },
    );
}
