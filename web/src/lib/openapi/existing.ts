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

// ── search (SEARCH S1 #83, consolidated S2+S3+S6+S7, hybrid S5 #122) ───────
// GET /api/search — cross-project library search. Documents the consolidated
// shape from web/src/app/api/search/route.ts, which reconciles five tickets
// built against the S1 MVP: the Postgres FTS index (S2 #119), library-wide
// semantic search (S3 #120), result enrichment — duration/highlighted
// snippets/thumbnails (S6 #123), filters/facets (S7 #124), and hybrid RRF
// ranking (S5 #122). See docs/cross_project_search.md for the full narrative.
//
// ⚠️ BEHAVIOR CHANGE (S5 #122): the default `mode` changed from "keyword" to
// "hybrid". Existing callers that relied on the implicit keyword default
// (i.e. never passed `mode`) will now get hybrid-fused results instead —
// pass `mode=keyword` explicitly to keep the old behavior byte-for-byte.
export const SearchQuery = z
  .object({
    q: z.string().min(1).openapi({
      description: "Search text (required). Tokenized into lowercased terms.",
      example: "ceiling",
    }),
    limit: z.coerce
      .number()
      .int()
      .min(1)
      .max(50)
      .default(20)
      .optional()
      .openapi({
        description: "Max hits to return (default 20, capped at 50).",
      }),
    mode: z
      .enum(["hybrid", "keyword", "semantic"])
      .default("hybrid")
      .optional()
      .openapi({
        description:
          "\"hybrid\" (default, S5 #122) fuses the keyword and semantic " +
          "channels with Reciprocal Rank Fusion (k=60) into one ranked, " +
          "deduped (one hit per clip_id) list; each hit's `sources` says " +
          "which channel(s) it came from. \"keyword\" (S2 #119) ranks via " +
          "Postgres full-text search (search_library_fts) only. \"semantic\" " +
          "(S3 #120) embeds `q` and cosine-searches clip_embeddings only " +
          "(and, unlike the other two modes, can return multiple hits per " +
          "clip — one per matched frame). Any unrecognized value falls back " +
          "to \"hybrid\". ⚠️ Prior to S5, the default (no `mode` param) was " +
          "\"keyword\" — pass `mode=keyword` explicitly to keep that exact " +
          "behavior.",
      }),
    pooled: z
      .enum(["0", "1"])
      .default("0")
      .optional()
      .openapi({
        description:
          "Semantic mode only: \"1\" to also match whole-clip pooled vectors " +
          "(default: frames only).",
      }),
    thumbnails: z
      .enum(["0", "1"])
      .default("0")
      .optional()
      .openapi({
        description:
          "\"1\" to include a signed `thumbnail_url` per result (S6 #123). " +
          "Opt-in — costs one Modal frame-extraction call per result.",
      }),
    project: z
      .array(z.string())
      .optional()
      .openapi({
        description:
          "Restrict to one/several projects (S7 #124); repeatable or " +
          "comma-separated. Each token matches a project id or " +
          "case-insensitive name. Omit for all of the caller's projects.",
      }),
    kind: z
      .enum(["speech", "visual", "both"])
      .default("both")
      .optional()
      .openapi({
        description:
          "\"speech\" restricts to transcript matches; \"visual\" restricts " +
          "to visual_description/highlight (and, in semantic mode, embedding) " +
          "matches (S7 #124).",
      }),
    min_duration: z.coerce.number().min(0).optional().openapi({
      description: "Minimum clip length in seconds (S7 #124).",
    }),
    max_duration: z.coerce.number().min(0).optional().openapi({
      description: "Maximum clip length in seconds (S7 #124).",
    }),
    since: z.string().optional().openapi({
      description:
        "ISO date/timestamp lower bound on clip recorded_at (falling back " +
        "to created_at) (S7 #124).",
    }),
    until: z.string().optional().openapi({
      description: "ISO date/timestamp upper bound, same field as `since` (S7 #124).",
    }),
  })
  .openapi("SearchQuery");

export const SearchHitKind = z
  .enum(["transcript", "visual_description", "highlight", "embedding"])
  .openapi("SearchHitKind");

export const SearchHitSource = z
  .enum(["keyword", "semantic"])
  .openapi("SearchHitSource");

export const SearchHit = z
  .object({
    clip_id: z.string().uuid().openapi({
      description:
        "Global clip id. Use directly as a cross-project timeline video " +
        "item's `clip_id` (see docs/cross_project_editing.md).",
    }),
    project: z.string().nullable().openapi({ description: "Owning project name." }),
    project_id: z.string().uuid(),
    filename: z.string().nullable(),
    duration: z.number().nullable().openapi({
      description:
        "clips.duration_secs (S6 #123) — validate/clamp a proposed src_end " +
        "without a follow-up lookup.",
    }),
    kind: SearchHitKind,
    timestamp: z.number().nullable().openapi({
      description:
        "Clip-local seconds for the best-matching moment (null for a whole-clip " +
        "match, e.g. kind=visual_description). Use as `src_start` (pick a small " +
        "window around it for `src_end`).",
    }),
    snippet: z.string().openapi({
      description:
        "Keyword mode: Postgres ts_headline output, matched terms already " +
        "wrapped in **markers** (stemmed matches included). Semantic mode: a " +
        "synthetic \"frame @ Xs (semantic match)\" / \"whole-clip semantic " +
        "match\" placeholder (there's no textual match to highlight).",
    }),
    thumbnail_url: z.string().url().nullable().optional().openapi({
      description:
        "Signed frame URL at `timestamp` (S6 #123), only present when " +
        "`?thumbnails=1` was requested. null on a per-thumbnail failure.",
    }),
    score: z.number().openapi({
      description:
        "Keyword mode: ts_rank_cd. Semantic mode: raw cosine similarity in " +
        "[0, 1]. Hybrid mode: the fused Reciprocal Rank Fusion score, " +
        "Σ 1/(60 + rank) over the channel(s) the clip appeared in — not on " +
        "the same scale as either channel's solo score.",
    }),
    sources: z.array(SearchHitSource).min(1).openapi({
      description:
        "Which channel(s) produced this hit (S5 #122). In `mode=keyword`/" +
        "`mode=semantic` this is always the single requesting channel; in " +
        "`mode=hybrid` a clip matched by both channels carries " +
        "[\"keyword\", \"semantic\"] and its other fields (kind/timestamp/" +
        "snippet) come from whichever channel ranked it better.",
    }),
  })
  .openapi("SearchHit");

export const SearchFacets = z
  .object({
    by_project: z.record(z.number().int()).openapi({
      description: "Hit count per project name (or project_id if unnamed).",
    }),
    by_kind: z.record(z.number().int()).openapi({
      description: "Hit count per SearchHitKind value.",
    }),
  })
  .openapi("SearchFacets");

export const SearchResponse = z
  .object({
    query: z.string(),
    terms: z.array(z.string()).openapi({
      description:
        "Tokenized query terms (keyword and hybrid modes; always [] in " +
        "semantic mode, since it doesn't tokenize `q`).",
    }),
    count: z.number().int(),
    results: z.array(SearchHit),
    facets: SearchFacets.openapi({
      description:
        "Counted over the full filtered (and, in hybrid mode, fused/deduped) " +
        "candidate set, before slicing to `limit` (S7 #124).",
    }),
    mode: z.enum(["hybrid", "keyword", "semantic"]).openapi({
      description: "Which mode actually served this request (S5 #122).",
    }),
    warnings: z.array(z.string()).optional().openapi({
      description:
        "Hybrid mode only: present when the semantic channel errored and " +
        "the response degraded to keyword-only ranking rather than failing " +
        "the request (S5 #122).",
    }),
  })
  .openapi("SearchResponse");
