"use client";

import { useState } from "react";

export interface ApiKeyRow {
  id: string;
  label: string | null;
  key_prefix: string;
  created_at: string;
  last_used_at: string | null;
}

export function ApiKeysManager({ initialKeys }: { initialKeys: ApiKeyRow[] }) {
  const [keys, setKeys] = useState<ApiKeyRow[]>(initialKeys);
  const [label, setLabel] = useState("");
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [newKey, setNewKey] = useState<string | null>(null);

  async function createKey(e: React.FormEvent) {
    e.preventDefault();
    setCreating(true);
    setError(null);
    setNewKey(null);
    try {
      const res = await fetch("/api/keys", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ label: label.trim() || undefined }),
      });
      const data = await res.json();
      if (!res.ok) throw new Error(data.error ?? "Failed to create key");
      setNewKey(data.key as string);
      setKeys((prev) => [
        {
          id: data.id,
          label: data.label,
          key_prefix: data.key_prefix,
          created_at: data.created_at,
          last_used_at: null,
        },
        ...prev,
      ]);
      setLabel("");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to create key");
    } finally {
      setCreating(false);
    }
  }

  async function revokeKey(id: string) {
    if (!confirm("Revoke this key? Anything using it will stop working.")) return;
    const res = await fetch(`/api/keys/${id}`, { method: "DELETE" });
    if (res.ok) setKeys((prev) => prev.filter((k) => k.id !== id));
    else setError("Failed to revoke key");
  }

  return (
    <div className="flex flex-col gap-4">
      <form onSubmit={createKey} className="flex gap-2">
        <input
          type="text"
          value={label}
          onChange={(e) => setLabel(e.target.value)}
          placeholder="Key name (optional)"
          maxLength={100}
          className="flex-1 rounded-xl border border-zinc-200 bg-white px-3 py-2 text-sm dark:border-zinc-800 dark:bg-zinc-900"
        />
        <button
          type="submit"
          disabled={creating}
          className="rounded-xl bg-zinc-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-50 dark:bg-white dark:text-zinc-900"
        >
          {creating ? "Creating…" : "New key"}
        </button>
      </form>

      {error && <p className="text-sm text-red-500">{error}</p>}

      {newKey && (
        <div className="rounded-xl border border-green-300 bg-green-50 p-3 dark:border-green-900 dark:bg-green-950">
          <p className="text-xs font-medium text-green-800 dark:text-green-300">
            Copy this key now — you won&apos;t be able to see it again.
          </p>
          <div className="mt-2 flex items-center gap-2">
            <code className="flex-1 break-all rounded-lg bg-white px-2 py-1 text-xs dark:bg-zinc-900">
              {newKey}
            </code>
            <button
              onClick={() => navigator.clipboard?.writeText(newKey)}
              className="shrink-0 rounded-lg border border-zinc-300 px-2 py-1 text-xs dark:border-zinc-700"
            >
              Copy
            </button>
          </div>
        </div>
      )}

      {keys.length > 0 ? (
        <ul className="flex flex-col gap-2">
          {keys.map((k) => (
            <li
              key={k.id}
              className="flex items-center justify-between rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
            >
              <div className="min-w-0">
                <p className="truncate text-sm font-medium">
                  {k.label || "Untitled key"}
                </p>
                <p className="text-xs text-zinc-500 dark:text-zinc-400">
                  {k.key_prefix}…{" · "}
                  {k.last_used_at
                    ? `last used ${new Date(k.last_used_at).toLocaleDateString()}`
                    : "never used"}
                </p>
              </div>
              <button
                onClick={() => revokeKey(k.id)}
                className="shrink-0 text-xs text-red-500 hover:underline"
              >
                Revoke
              </button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="py-4 text-center text-sm text-zinc-500 dark:text-zinc-400">
          No keys yet.
        </p>
      )}
    </div>
  );
}
