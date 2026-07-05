import { z, ErrorResponse } from "./registry";

// ─────────────────────────────────────────────────────────────────────────
// Schemas for the CORE EXISTING surface (roadmap Part B requirement: cover the
// foundation in the spec). These describe the current request/response shapes
// of routes that were written before the schema-first convention. They are NOT
// wired back into those handlers as runtime validators yet — they exist so the
// generated OpenAPI doc covers the surface an editing agent uses. New routes
// (the §1.3 perception tools) are authored Zod-first and colocated with their
// handlers; see web/src/app/api/clips/[id]/{frames,describe,contact-sheet}.
//
// Routes still to backfill into the spec (noted in the doc): clips/[id] GET,
// clips/[id]/{audio,audio-analysis,transcribe}, projects/[id] core CRUD,
// projects/[id]/{align,clips,generate,merge,renders,storage}, keys/*,
// stories/[id] GET + {clean-speech,revisions,timeline,video}.
// ─────────────────────────────────────────────────────────────────────────

export { ErrorResponse };

// ── clips/[id]/analyze ─────────────────────────────────────────────────────
export const AnalyzeBody = z
  .object({
    variant: z
      .enum(["v2"])
      .default("v2")
      .openapi({
        description:
          "Visual-analysis variant. Only the unified \"v2\" analysis is supported. " +
          "The retired A/B-era names (context, flash, flash_lowres, pro, editorial, " +
          "audio_aware, with_transcript, grounded) are still accepted for a deprecation " +
          "window: they are mapped to \"v2\" and the response carries a `Warning` header.",
      }),
  })
  .openapi("AnalyzeBody");

export const AnalyzeResponse = z
  .object({
    id: z.string().uuid(),
    variant: z.string(),
    status: z.string(),
  })
  .openapi("AnalyzeResponse");

// ── clips/[id]/visual ──────────────────────────────────────────────────────
export const VisualAnalysis = z
  .object({
    id: z.string().uuid(),
    variant: z.string(),
    status: z.string(),
    result: z.unknown().nullable(),
    result_r2_key: z.string().nullable().optional(),
    debug: z.unknown().nullable().optional(),
    error: z.string().nullable(),
    created_at: z.string(),
  })
  .openapi("VisualAnalysis");

export const VisualResponse = z
  .object({
    clip: z.object({
      id: z.string().uuid(),
      filename: z.string().nullable(),
      duration_secs: z.number().nullable(),
      status: z.string().nullable(),
      r2_key: z.string().nullable(),
    }),
    analyses: z.array(VisualAnalysis),
  })
  .openapi("VisualResponse");

// ── shared range/overlay ───────────────────────────────────────────────────
export const Range = z
  .object({
    source: z.string().openapi({ description: "Clip filename or 'blank'." }),
    start: z.number(),
    end: z.number(),
    overlay: z.record(z.unknown()).optional(),
  })
  .openapi("Range");

// ── projects/[id]/stories (GET list + POST create) ─────────────────────────
export const Story = z
  .object({
    id: z.string().uuid(),
    title: z.string().nullable(),
    description: z.string().nullable(),
    estimated_duration_secs: z.number().nullable(),
    ranges_json: z.unknown(),
    status: z.string(),
    error_message: z.string().nullable(),
    created_at: z.string(),
  })
  .openapi("Story");

export const StoriesListResponse = z
  .object({
    rounds: z.array(
      z.object({
        id: z.string().uuid(),
        round: z.number(),
        prompt: z.string().nullable(),
        created_at: z.string(),
        stories: z.array(Story),
      }),
    ),
  })
  .openapi("StoriesListResponse");

export const CreateStoryBody = z
  .object({
    ranges: z.array(Range).min(1),
    title: z.string().optional(),
    description: z.string().optional(),
  })
  .openapi("CreateStoryBody");

export const CreateStoryResponse = z
  .object({
    id: z.string().uuid(),
    status: z.string(),
  })
  .openapi("CreateStoryResponse");

// ── projects/[id]/transcript ───────────────────────────────────────────────
export const TranscriptWord = z
  .object({
    text: z.string(),
    global_start: z.number().optional(),
    global_end: z.number().optional(),
    local_start: z.number().optional(),
    local_end: z.number().optional(),
    source: z.string().optional(),
  })
  .openapi("TranscriptWord");

export const TranscriptResponse = z
  .object({ words: z.array(TranscriptWord) })
  .passthrough()
  .openapi("TranscriptResponse");

// ── stories/[id]/render ────────────────────────────────────────────────────
export const RenderBody = z
  .object({
    ranges: z.array(Range).optional().openapi({
      description:
        "Optional updated ranges. Empty body re-renders current timeline_json.",
    }),
  })
  .openapi("RenderBody");

export const RenderResponse = z
  .object({ id: z.string().uuid(), status: z.string() })
  .openapi("RenderResponse");

// ── stories/[id]/edit ──────────────────────────────────────────────────────
export const EditBody = z
  .object({
    ops: z
      .array(z.record(z.unknown()))
      .optional()
      .openapi({
        description:
          "Edit-op list (trim, set_speed, set_mute, add_text, …). Empty " +
          "materializes the timeline from ranges_json. See docs/timeline_editing.md.",
      }),
    restore_revision: z.number().int().optional(),
  })
  .openapi("EditBody");

export const EditResponse = z
  .object({})
  .passthrough()
  .openapi("EditResponse");

// ── stories/[id]/signed-url ────────────────────────────────────────────────
export const SignedUrlResponse = z
  .object({
    playback_url: z.string().url(),
    download_url: z.string().url(),
  })
  .openapi("SignedUrlResponse");
