import { resolveAuth } from "@/lib/auth/resolve";
import { S3Client, GetObjectCommand } from "@aws-sdk/client-s3";

export const runtime = "nodejs";

function r2Client() {
  return new S3Client({
    region: "auto",
    endpoint: process.env.R2_ENDPOINT!,
    credentials: {
      accessKeyId: process.env.CLOUDFLARE_R2_ACCESS_KEY_ID!,
      secretAccessKey: process.env.CLOUDFLARE_R2_SECRET_ACCESS_KEY!,
    },
  });
}

// GET /api/stories/[id]/video
// Authenticated same-origin proxy for the rendered MP4.
// Lets the browser fetch() the video as a blob without hitting R2 CORS restrictions
// (R2 presigned URLs work for <video src> but not fetch() — no CORS headers).
export async function GET(
  request: Request,
  context: { params: Promise<{ id: string }> },
) {
  const { id: storyId } = await context.params;

  // API key (Bearer lsk_...) or browser session — agents can fetch renders too.
  const auth = await resolveAuth(request);
  if (!auth) {
    return new Response("Unauthorized", { status: 401 });
  }
  const { supabase } = auth;

  const { data: story } = await supabase
    .from("stories")
    .select("render_r2_key")
    .eq("id", storyId)
    .maybeSingle();

  if (!story?.render_r2_key) {
    return new Response("Not found", { status: 404 });
  }

  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    return new Response("R2 not configured", { status: 500 });
  }

  const cmd = new GetObjectCommand({ Bucket: bucket, Key: story.render_r2_key });
  const r2res = await r2Client().send(cmd);

  if (!r2res.Body) {
    return new Response("Empty body from R2", { status: 502 });
  }

  const headers: HeadersInit = { "Content-Type": "video/mp4" };
  if (r2res.ContentLength) {
    headers["Content-Length"] = String(r2res.ContentLength);
  }

  return new Response(r2res.Body.transformToWebStream(), { headers });
}
