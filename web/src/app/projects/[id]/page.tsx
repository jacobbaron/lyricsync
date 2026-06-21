export const dynamic = "force-dynamic";

import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { StatusPoller, type ProjectStatus, type ClipStatus } from "./StatusPoller";
import { StoragePanel } from "./StoragePanel";

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: projectId } = await params;

  const supabase = await createClient();
  const { data: project, error } = await supabase
    .from("projects")
    .select("id, name, status, error_message, created_at, clips(id, filename, status, error_message, duration_secs)")
    .eq("id", projectId)
    .maybeSingle();

  if (error) {
    console.error("Project fetch error:", error.message);
    throw new Error(error.message);
  }
  if (!project) notFound();

  const clips = (project.clips ?? []) as Array<{
    id: string;
    filename: string;
    status: string;
    error_message?: string | null;
    duration_secs?: number | null;
  }>;

  return (
    <main className="flex flex-col min-h-full">
      <header className="flex items-center gap-3 px-4 py-4 border-b border-zinc-200 dark:border-zinc-800">
        <Link
          href="/"
          className="text-zinc-500 dark:text-zinc-400 text-sm"
          aria-label="Back"
        >
          ←
        </Link>
        <h1 className="text-base font-semibold truncate">{project.name}</h1>
      </header>

      <div className="flex flex-col gap-5 px-4 py-6 max-w-lg mx-auto w-full">
        <StatusPoller
          projectId={projectId}
          initialProject={{
            id: project.id,
            name: project.name,
            status: project.status as ProjectStatus,
            error_message: project.error_message,
          }}
          initialClips={clips.map((c) => ({
            id: c.id,
            filename: c.filename,
            status: c.status as ClipStatus,
            error_message: c.error_message,
            duration_secs: c.duration_secs,
          }))}
        />

        <StoragePanel projectId={projectId} />
      </div>
    </main>
  );
}
