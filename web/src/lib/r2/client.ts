import { S3Client, PutObjectCommand, GetObjectCommand } from "@aws-sdk/client-s3";
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
