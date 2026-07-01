import {
  S3Client,
  PutObjectCommand,
  GetObjectCommand,
  HeadObjectCommand,
  DeleteObjectsCommand,
} from "@aws-sdk/client-s3";
import { getSignedUrl } from "@aws-sdk/s3-request-presigner";

export const UPLOAD_URL_TTL_SECONDS = 3600;

let cachedClient: S3Client | null = null;

function getClient(): S3Client {
  if (cachedClient) return cachedClient;

  const endpoint = process.env.R2_ENDPOINT;
  const accessKeyId = process.env.CLOUDFLARE_R2_ACCESS_KEY_ID;
  const secretAccessKey = process.env.CLOUDFLARE_R2_SECRET_ACCESS_KEY;

  if (!endpoint || !accessKeyId || !secretAccessKey) {
    const missing = [
      !endpoint && "R2_ENDPOINT",
      !accessKeyId && "CLOUDFLARE_R2_ACCESS_KEY_ID",
      !secretAccessKey && "CLOUDFLARE_R2_SECRET_ACCESS_KEY",
    ]
      .filter(Boolean)
      .join(", ");
    throw new Error(
      `R2 client misconfigured: ${missing} not set. Check Vercel env vars for this environment.`,
    );
  }

  cachedClient = new S3Client({
    region: "auto",
    endpoint,
    credentials: { accessKeyId, secretAccessKey },
  });
  return cachedClient;
}

export function clipObjectKey(
  projectId: string,
  clipId: string,
  extension: string,
): string {
  return `projects/${projectId}/clips/${clipId}/original.${extension}`;
}

export function songObjectKey(
  projectId: string,
  songId: string,
  extension: string,
): string {
  return `projects/${projectId}/songs/${songId}/original.${extension}`;
}

export async function presignClipUpload(
  key: string,
  contentType: string,
): Promise<string> {
  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    throw new Error(
      "R2 client misconfigured: R2_BUCKET_NAME not set. Check Vercel env vars for this environment.",
    );
  }
  const cmd = new PutObjectCommand({
    Bucket: bucket,
    Key: key,
    ContentType: contentType,
  });
  return getSignedUrl(getClient(), cmd, { expiresIn: UPLOAD_URL_TTL_SECONDS });
}

/** Generate a presigned GET URL for reading an object.
 *
 * @param key               R2 object key
 * @param expiresIn         TTL in seconds (default 3600)
 * @param contentDisposition  Optional Content-Disposition override, e.g.
 *                            `attachment; filename="output.mp4"` — tells the
 *                            browser to save the file rather than play it.
 */
export async function presignDownload(
  key: string,
  expiresIn = 3600,
  contentDisposition?: string,
): Promise<string> {
  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    throw new Error("R2 client misconfigured: R2_BUCKET_NAME not set.");
  }
  const cmd = new GetObjectCommand({
    Bucket: bucket,
    Key: key,
    ...(contentDisposition
      ? { ResponseContentDisposition: contentDisposition }
      : {}),
  });
  return getSignedUrl(getClient(), cmd, { expiresIn });
}

/** Upload a JSON-serialisable value to R2. */
export async function putObjectJson(key: string, data: unknown): Promise<void> {
  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    throw new Error("R2 client misconfigured: R2_BUCKET_NAME not set.");
  }
  const cmd = new PutObjectCommand({
    Bucket: bucket,
    Key: key,
    Body: JSON.stringify(data),
    ContentType: "application/json",
  });
  await getClient().send(cmd);
}

/** Return the size in bytes of an R2 object, or null if it no longer exists.
 *
 * Used by the storage dashboard to total usage without persisting sizes in the
 * DB. A missing object (deleted out-of-band, or a row whose upload never
 * completed) returns null rather than throwing, so one stale key doesn't break
 * the whole rollup. */
export async function headObjectSize(key: string): Promise<number | null> {
  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    throw new Error("R2 client misconfigured: R2_BUCKET_NAME not set.");
  }
  try {
    const res = await getClient().send(
      new HeadObjectCommand({ Bucket: bucket, Key: key }),
    );
    return res.ContentLength ?? null;
  } catch (err) {
    const status = (err as { $metadata?: { httpStatusCode?: number } })
      ?.$metadata?.httpStatusCode;
    const name = (err as { name?: string })?.name;
    if (status === 404 || name === "NotFound" || name === "NoSuchKey") {
      return null;
    }
    throw err;
  }
}

/** Delete one or more objects from R2. No-op on an empty list.
 *
 * S3's DeleteObjects caps each request at 1000 keys, so larger lists are
 * chunked. Deleting a non-existent key is not an error. */
export async function deleteObjects(
  keys: (string | null | undefined)[],
): Promise<void> {
  const present = keys.filter((k): k is string => Boolean(k));
  if (present.length === 0) return;

  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    throw new Error("R2 client misconfigured: R2_BUCKET_NAME not set.");
  }
  const client = getClient();
  for (let i = 0; i < present.length; i += 1000) {
    const chunk = present.slice(i, i + 1000);
    await client.send(
      new DeleteObjectsCommand({
        Bucket: bucket,
        Delete: { Objects: chunk.map((Key) => ({ Key })), Quiet: true },
      }),
    );
  }
}

/** Fetch an object from R2 and return its body as a string. */
export async function getObjectText(key: string): Promise<string> {
  const bucket = process.env.R2_BUCKET_NAME;
  if (!bucket) {
    throw new Error("R2 client misconfigured: R2_BUCKET_NAME not set.");
  }
  const cmd = new GetObjectCommand({ Bucket: bucket, Key: key });
  const res = await getClient().send(cmd);
  if (!res.Body) throw new Error(`Empty body for R2 key: ${key}`);
  return res.Body.transformToString("utf-8");
}
