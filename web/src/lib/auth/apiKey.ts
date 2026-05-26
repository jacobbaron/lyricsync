import { createHash, createHmac, randomBytes } from "crypto";

const KEY_PREFIX = "lsk_";

// Generates a new API key. The plaintext is returned to the caller exactly once
// (to show the user); only the hash and a display prefix are persisted.
export function generateApiKey() {
  const plaintext = KEY_PREFIX + randomBytes(32).toString("base64url");
  return {
    plaintext,
    hash: hashApiKey(plaintext),
    prefix: plaintext.slice(0, KEY_PREFIX.length + 8),
  };
}

export function hashApiKey(plaintext: string): string {
  return createHash("sha256").update(plaintext).digest("hex");
}

export function isApiKey(token: string): boolean {
  return token.startsWith(KEY_PREFIX);
}

function base64url(input: string): string {
  return Buffer.from(input).toString("base64url");
}

// Mints a short-lived Supabase-compatible JWT (HS256, signed with the project's
// JWT secret) carrying the owner's email. RLS reads `auth.jwt() ->> 'email'`,
// so a request bearing this token is scoped to the owner just like a browser
// session. The token is never stored — it is minted fresh per request.
export function mintUserJwt(opts: { email: string; sub: string }): string {
  const secret = process.env.SUPABASE_JWT_SECRET;
  if (!secret) {
    throw new Error("SUPABASE_JWT_SECRET not set — cannot mint API-key session");
  }

  const now = Math.floor(Date.now() / 1000);
  const header = base64url(JSON.stringify({ alg: "HS256", typ: "JWT" }));
  const payload = base64url(
    JSON.stringify({
      sub: opts.sub,
      email: opts.email,
      role: "authenticated",
      aud: "authenticated",
      iss: "supabase",
      iat: now,
      exp: now + 300, // 5 minutes; minted fresh on every request
    }),
  );
  const data = `${header}.${payload}`;
  const signature = createHmac("sha256", secret).update(data).digest("base64url");
  return `${data}.${signature}`;
}
