export const runtime = "nodejs";

// ── GET /api/docs ──────────────────────────────────────────────────────────
// Swagger UI for the LyricSync API, loading /api/openapi.json. Served as a
// static HTML shell that pulls Swagger UI from a CDN — no extra npm dep, and
// the spec itself (/api/openapi.json) stays the must-have artifact.

const HTML = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1" />
    <title>LyricSync API</title>
    <link rel="stylesheet" href="https://unpkg.com/swagger-ui-dist@5/swagger-ui.css" />
    <style>body { margin: 0 }</style>
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="https://unpkg.com/swagger-ui-dist@5/swagger-ui-bundle.js" crossorigin></script>
    <script>
      window.ui = SwaggerUIBundle({
        url: "/api/openapi.json",
        dom_id: "#swagger-ui",
      });
    </script>
  </body>
</html>`;

export async function GET() {
  return new Response(HTML, {
    headers: { "Content-Type": "text/html; charset=utf-8" },
  });
}
