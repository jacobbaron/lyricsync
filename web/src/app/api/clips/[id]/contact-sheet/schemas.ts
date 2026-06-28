import { z } from "@/lib/openapi/registry";

// ── GET /api/clips/{id}/contact-sheet?start=&end=&cols=&rows= ──────────────

export const ContactSheetQuery = z
  .object({
    start: z.coerce
      .number()
      .min(0)
      .default(0)
      .openapi({ description: "Window start, seconds within the clip." }),
    end: z.coerce
      .number()
      .positive()
      .optional()
      .openapi({
        description: "Window end, seconds. Defaults to the clip duration.",
      }),
    cols: z.coerce
      .number()
      .int()
      .min(1)
      .max(8)
      .default(4)
      .openapi({ description: "Grid columns (1–8)." }),
    rows: z.coerce
      .number()
      .int()
      .min(1)
      .max(8)
      .default(4)
      .openapi({ description: "Grid rows (1–8)." }),
  })
  .openapi("ContactSheetQuery");

export const ContactSheetResponse = z
  .object({
    clip_id: z.string().uuid(),
    start: z.number(),
    end: z.number(),
    cols: z.number().int(),
    rows: z.number().int(),
    url: z
      .string()
      .url()
      .openapi({ description: "Signed R2 URL for the tiled JPEG (≈1h TTL)." }),
    cached: z.boolean(),
  })
  .openapi("ContactSheetResponse");

export type ContactSheetQuery = z.infer<typeof ContactSheetQuery>;
export type ContactSheetResponse = z.infer<typeof ContactSheetResponse>;
