"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";

export function NewProjectForm() {
  const [open, setOpen] = useState(false);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [isPending, startTransition] = useTransition();
  const router = useRouter();

  function handleOpen() {
    setOpen(true);
    setError(null);
  }

  function handleCancel() {
    setOpen(false);
    setName("");
    setError(null);
  }

  function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    const trimmed = name.trim();
    if (!trimmed) return;

    startTransition(async () => {
      setError(null);
      const res = await fetch("/api/projects", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: trimmed }),
      });

      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        setError(body.error ?? "Failed to create project");
        return;
      }

      const project = await res.json();
      router.push(`/projects/${project.id}`);
    });
  }

  if (!open) {
    return (
      <button
        onClick={handleOpen}
        className="w-full rounded-xl bg-zinc-900 px-4 py-3 text-sm font-medium text-white dark:bg-white dark:text-zinc-900"
      >
        + New Project
      </button>
    );
  }

  return (
    <form onSubmit={handleSubmit} className="flex flex-col gap-3">
      <input
        autoFocus
        type="text"
        placeholder="Project name"
        value={name}
        onChange={(e) => setName(e.target.value)}
        maxLength={200}
        className="w-full rounded-xl border border-zinc-200 bg-white px-4 py-3 text-sm outline-none focus:border-zinc-400 dark:border-zinc-700 dark:bg-zinc-900 dark:text-white dark:focus:border-zinc-500"
      />
      {error && <p className="text-xs text-red-500">{error}</p>}
      <div className="flex gap-2">
        <button
          type="button"
          onClick={handleCancel}
          disabled={isPending}
          className="flex-1 rounded-xl border border-zinc-200 py-3 text-sm dark:border-zinc-700"
        >
          Cancel
        </button>
        <button
          type="submit"
          disabled={isPending || !name.trim()}
          className="flex-1 rounded-xl bg-zinc-900 py-3 text-sm font-medium text-white disabled:opacity-40 dark:bg-white dark:text-zinc-900"
        >
          {isPending ? "Creating…" : "Create"}
        </button>
      </div>
    </form>
  );
}
