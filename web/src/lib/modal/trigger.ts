import { after } from "next/server";

/**
 * Fire-and-forget POST to a Modal endpoint with webhook-secret auth.
 * Logs success/failure but never throws; returns immediately.
 *
 * Used by routes that don't need the Modal response (render, transcribe,
 * generate, align, music, lipsync, align-song, stories POST).
 *
 * @param tag   Short label for console logs (e.g. "render", "music").
 * @param url   Modal endpoint URL (from env var).
 * @param body  JSON payload for the Modal worker.
 */
export function triggerModal(
  tag: string,
  url: string | undefined,
  body: Record<string, unknown>,
): void {
  const secret = process.env.MODAL_WEBHOOK_SECRET;
  if (!url || !secret) {
    console.warn(`[${tag}] Modal URL or MODAL_WEBHOOK_SECRET not set — not triggered`);
    return;
  }
  fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-webhook-secret": secret,
    },
    body: JSON.stringify(body),
  }).catch((err) => console.error(`[${tag}] Modal trigger failed:`, err));
}

/**
 * Deferred POST to a Modal endpoint via Next.js `after()`: the 202 response
 * goes out immediately while Vercel keeps the function alive to complete the
 * fetch (handles cold-start latency on isolated Modal images).
 *
 * Used by signal-creation routes (quality, detect, embed, motion) and the
 * analyze route.
 *
 * @param tag   Short label for console logs.
 * @param url   Modal endpoint URL (from env var / fallback).
 * @param body  JSON payload for the Modal worker.
 */
export function deferModal(
  tag: string,
  url: string,
  body: Record<string, unknown>,
): void {
  const secret = process.env.MODAL_WEBHOOK_SECRET;
  if (!secret) {
    console.warn(`[${tag}] MODAL_WEBHOOK_SECRET not set — not triggered`);
    return;
  }
  after(async () => {
    try {
      const res = await fetch(url, {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "x-webhook-secret": secret,
        },
        body: JSON.stringify(body),
      });
      const text = await res.text().catch(() => "");
      console.log(`[${tag}] Modal responded ${res.status}: ${text}`);
      if (!res.ok) {
        console.error(`[${tag}] Modal error for ${JSON.stringify(body)}: ${text}`);
      }
    } catch (err) {
      console.error(`[${tag}] Modal trigger failed:`, err);
    }
  });
}

/**
 * Synchronous POST to a Modal endpoint — relays the upstream JSON response
 * and status code verbatim. Used by routes that proxy to Modal and return
 * the result inline (edit, clean-speech, audio-analysis).
 *
 * @returns A `{ payload, status }` pair ready to be passed to NextResponse.json().
 */
export async function proxyModal(
  url: string,
  body: Record<string, unknown>,
  fallbackErrorLabel = "service",
): Promise<{ payload: unknown; status: number }> {
  const secret = process.env.MODAL_WEBHOOK_SECRET;
  if (!secret) {
    return {
      payload: { error: "MODAL_WEBHOOK_SECRET not configured" },
      status: 500,
    };
  }
  const upstream = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-webhook-secret": secret,
    },
    body: JSON.stringify(body),
  });
  const payload = await upstream.json().catch(() => ({
    error: `${fallbackErrorLabel} returned a non-JSON response`,
  }));
  return { payload, status: upstream.status };
}
