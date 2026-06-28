import { z } from "zod";
import { extendZodWithOpenApi } from "@asteasolutions/zod-to-openapi";

// Teach Zod the `.openapi()` method so schemas can carry OpenAPI metadata.
// Must run before any schema calls `.openapi(...)`, so every schema module
// imports `z` from here rather than from "zod" directly.
extendZodWithOpenApi(z);

export { z };

// Shared error envelope used by every route on auth/validation failure.
export const ErrorResponse = z
  .object({ error: z.string() })
  .openapi("ErrorResponse");
