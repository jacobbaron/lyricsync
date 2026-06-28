import {
  OpenApiGeneratorV31,
  OpenAPIRegistry,
} from "@asteasolutions/zod-to-openapi";
import { z, ErrorResponse } from "./registry";

// New §1.3 perception endpoints — authored Zod-first, colocated with handlers.
import {
  FramesQuery,
  FramesResponse,
} from "@/app/api/clips/[id]/frames/schemas";
import {
  DescribeBody,
  DescribeResponse,
} from "@/app/api/clips/[id]/describe/schemas";
import {
  ContactSheetQuery,
  ContactSheetResponse,
} from "@/app/api/clips/[id]/contact-sheet/schemas";

// Core existing surface (foundation coverage; see existing.ts header note).
import {
  AnalyzeBody,
  AnalyzeResponse,
  VisualResponse,
  StoriesListResponse,
  CreateStoryBody,
  CreateStoryResponse,
  TranscriptResponse,
  RenderBody,
  RenderResponse,
  EditBody,
  EditResponse,
  SignedUrlResponse,
} from "./existing";

// Builds the OpenAPI 3.1 document from the registered Zod schemas. Single
// source of truth: every request/response shape here is the same schema the
// route handler uses (for the new routes) or describes the live shape (for the
// existing routes being backfilled).
export function buildOpenApiDocument() {
  const registry = new OpenAPIRegistry();

  const bearerAuth = registry.registerComponent("securitySchemes", "bearerAuth", {
    type: "http",
    scheme: "bearer",
    bearerFormat: "lsk_…",
    description:
      "LyricSync API key. Send as `Authorization: Bearer lsk_…`. A browser " +
      "session cookie also authenticates these routes.",
  });
  const security = [{ [bearerAuth.name]: [] }];

  const clipIdParam = z
    .string()
    .uuid()
    .openapi({ param: { name: "id", in: "path" }, example: "clip-uuid" });
  const projectIdParam = z
    .string()
    .uuid()
    .openapi({ param: { name: "id", in: "path" } });
  const storyIdParam = z
    .string()
    .uuid()
    .openapi({ param: { name: "id", in: "path" } });

  const json = (schema: z.ZodTypeAny) => ({
    "application/json": { schema },
  });
  const errors = {
    400: { description: "Bad request", content: json(ErrorResponse) },
    401: { description: "Unauthorized", content: json(ErrorResponse) },
    404: { description: "Not found", content: json(ErrorResponse) },
  };

  // ── §1.3 perception tools ────────────────────────────────────────────────
  registry.registerPath({
    method: "get",
    path: "/api/clips/{id}/frames",
    summary: "Extract N frames from a clip",
    description:
      "Interactive perception (roadmap §1.3): fast-seek N frames so an editing " +
      "agent can look at exact moments. Cached by (clip, t, n, interval).",
    tags: ["perception"],
    security,
    request: {
      params: z.object({ id: clipIdParam }),
      query: FramesQuery,
    },
    responses: {
      200: { description: "Signed frame URLs", content: json(FramesResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "post",
    path: "/api/clips/{id}/describe",
    summary: "Ask Gemini about a clip sub-range",
    description:
      "Interactive perception (roadmap §1.3): Gemini Flash answers a question " +
      "about JUST [start, end] of a clip. Cached by (clip, start, end, question).",
    tags: ["perception"],
    security,
    request: {
      params: z.object({ id: clipIdParam }),
      body: { content: json(DescribeBody) },
    },
    responses: {
      200: { description: "Gemini answer", content: json(DescribeResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "get",
    path: "/api/clips/{id}/contact-sheet",
    summary: "Tiled contact sheet across a range",
    description:
      "Interactive perception (roadmap §1.3): sample cols*rows frames across a " +
      "range, burn timestamps in, tile into one JPEG. Cached by " +
      "(clip, start, end, cols, rows).",
    tags: ["perception"],
    security,
    request: {
      params: z.object({ id: clipIdParam }),
      query: ContactSheetQuery,
    },
    responses: {
      200: {
        description: "Signed contact-sheet URL",
        content: json(ContactSheetResponse),
      },
      ...errors,
    },
  });

  // ── core existing surface ────────────────────────────────────────────────
  registry.registerPath({
    method: "post",
    path: "/api/clips/{id}/analyze",
    summary: "Kick off a visual-analysis run",
    tags: ["clips"],
    security,
    request: {
      params: z.object({ id: clipIdParam }),
      body: { content: json(AnalyzeBody) },
    },
    responses: {
      202: { description: "Analysis queued", content: json(AnalyzeResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "get",
    path: "/api/clips/{id}/visual",
    summary: "List visual-analysis runs for a clip",
    tags: ["clips"],
    security,
    request: { params: z.object({ id: clipIdParam }) },
    responses: {
      200: { description: "Analyses", content: json(VisualResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "get",
    path: "/api/projects/{id}/stories",
    summary: "List generation rounds + their stories",
    tags: ["stories"],
    security,
    request: { params: z.object({ id: projectIdParam }) },
    responses: {
      200: { description: "Rounds", content: json(StoriesListResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "post",
    path: "/api/projects/{id}/stories",
    summary: "Create a story from ranges and render it",
    tags: ["stories"],
    security,
    request: {
      params: z.object({ id: projectIdParam }),
      body: { content: json(CreateStoryBody) },
    },
    responses: {
      201: { description: "Story created", content: json(CreateStoryResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "get",
    path: "/api/projects/{id}/transcript",
    summary: "Project word-level transcript",
    tags: ["projects"],
    security,
    request: { params: z.object({ id: projectIdParam }) },
    responses: {
      200: { description: "Transcript", content: json(TranscriptResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "post",
    path: "/api/stories/{id}/render",
    summary: "(Re-)render a story",
    tags: ["stories"],
    security,
    request: {
      params: z.object({ id: storyIdParam }),
      body: { content: json(RenderBody) },
    },
    responses: {
      200: { description: "Render queued", content: json(RenderResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "post",
    path: "/api/stories/{id}/edit",
    summary: "Apply timeline edit ops",
    tags: ["stories"],
    security,
    request: {
      params: z.object({ id: storyIdParam }),
      body: { content: json(EditBody) },
    },
    responses: {
      200: { description: "Edited timeline", content: json(EditResponse) },
      ...errors,
    },
  });

  registry.registerPath({
    method: "get",
    path: "/api/stories/{id}/signed-url",
    summary: "Signed playback + download URLs",
    tags: ["stories"],
    security,
    request: { params: z.object({ id: storyIdParam }) },
    responses: {
      200: { description: "URLs", content: json(SignedUrlResponse) },
      409: { description: "Not ready", content: json(ErrorResponse) },
      ...errors,
    },
  });

  const generator = new OpenApiGeneratorV31(registry.definitions);
  return generator.generateDocument({
    openapi: "3.1.0",
    info: {
      title: "LyricSync API",
      version: "1.0.0",
      description:
        "LyricSync editing + perception API. The §1.3 interactive perception " +
        "tools (frames / describe / contact-sheet) are authored schema-first; " +
        "the core editing surface is documented here too. Some legacy routes " +
        "remain to backfill — see web/src/lib/openapi/existing.ts.",
    },
    servers: [{ url: "/", description: "Same origin" }],
  });
}
