import { z } from "@/lib/openapi/registry";

// ── GET /api/clips/{id}/frames — query params + response ───────────────────
// Authored Zod-first (roadmap §1.3): the schema is the source of truth for
// both runtime validation and the OpenAPI spec.

export const FramesQuery = z
  .object({
    t: z.coerce
      .number()
      .min(0)
      .default(0)
      .openapi({ description: "Start time in seconds within the clip." }),
    n: z.coerce
      .number()
      .int()
      .min(1)
      .max(30)
      .default(1)
      .openapi({ description: "Number of frames to extract (1–30)." }),
    interval: z.coerce
      .number()
      .positive()
      .default(1)
      .openapi({ description: "Seconds between successive frames." }),
  })
  .openapi("FramesQuery");

export const FrameItem = z
  .object({
    t: z.number().openapi({ description: "Exact time of this frame, seconds." }),
    url: z
      .string()
      .url()
      .openapi({ description: "Signed R2 URL for the JPEG (≈1h TTL)." }),
  })
  .openapi("FrameItem");

export const FramesResponse = z
  .object({
    clip_id: z.string().uuid(),
    frames: z.array(FrameItem),
    cached: z.boolean().openapi({
      description: "True when served from the clip_inspections cache.",
    }),
  })
  .openapi("FramesResponse");

export type FramesQuery = z.infer<typeof FramesQuery>;
export type FramesResponse = z.infer<typeof FramesResponse>;
