export const dynamic = "force-dynamic";

import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { ApiKeysManager, type ApiKeyRow } from "./ApiKeysManager";

export default async function ApiKeysPage() {
  const supabase = await createClient();
  const { data: keys } = await supabase
    .from("api_keys")
    .select("id, label, key_prefix, created_at, last_used_at")
    .is("revoked_at", null)
    .order("created_at", { ascending: false });

  return (
    <main className="flex flex-col min-h-full">
      <header className="flex items-center justify-between px-4 py-4 border-b border-zinc-200 dark:border-zinc-800">
        <h1 className="text-lg font-semibold tracking-tight">API keys</h1>
        <Link
          href="/"
          className="text-xs text-zinc-500 dark:text-zinc-400 hover:underline"
        >
          Back
        </Link>
      </header>

      <div className="flex flex-col gap-4 px-4 py-6 max-w-lg mx-auto w-full">
        <p className="text-sm text-zinc-500 dark:text-zinc-400">
          Personal keys for programmatic access (e.g. an MCP server). A key
          carries your identity and can read and edit only your projects. The
          secret is shown once at creation — store it somewhere safe.
        </p>
        <ApiKeysManager initialKeys={(keys ?? []) as ApiKeyRow[]} />
      </div>
    </main>
  );
}
