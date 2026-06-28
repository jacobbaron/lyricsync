import { z } from "@/lib/openapi/registry";

// ── POST /api/clips/{id}/describe — body + response ────────────────────────

export const DescribeBody = z
  .object({
    start: z
      .number()
      .min(0)
      .openapi({ description: "Sub-range start, seconds within the clip." }),
    end: z
      .number()
      .positive()
      .openapi({ description: "Sub-range end, seconds within the clip." }),
    question: z
      .string()
      .trim()
      .min(1)
      .optional()
      .openapi({
        description:
          "What to ask Gemini about the range. Defaults to a description of " +
          "who's on screen, expressions, actions, and shot type.",
      }),
  })
  .openapi("DescribeBody");

export const DescribeResponse = z
  .object({
    clip_id: z.string().uuid(),
    start: z.number(),
    end: z.number(),
    answer: z.string().openapi({ description: "Gemini's answer for the range." }),
    model: z.string(),
    cached: z.boolean(),
  })
  .openapi("DescribeResponse");

export type DescribeBody = z.infer<typeof DescribeBody>;
export type DescribeResponse = z.infer<typeof DescribeResponse>;
