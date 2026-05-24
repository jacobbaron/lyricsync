export const dynamic = "force-dynamic";

import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { StatusPoller, type ProjectStatus, type ClipStatus } from "./StatusPoller";

export default async function ProjectPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id: projectId } = await params;

  const supabase = await createClient();
  const { data: project, error } = await supabase
    .from("projects")
    .select("id, name, status, created_at, clips(id, filename, status, error_message)")
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
  }>;

  return (
    <main className="flex flex-col min-h-full">
      {/* Header — static, no need to re-render on each poll */}
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
        {/* StatusPoller handles:
              - Live clip list + per-clip status badges
              - Overall project status banner with progress count
              - Auto-triggering /align once all clips are transcribed_raw
              - Stopping polling when status is terminal
              - UploadArea (only while uploading)
              - Transcript viewer placeholder (once transcribed) */}
        <StatusPoller
          projectId={projectId}
          initialProject={{
            id: project.id,
            name: project.name,
            status: project.status as ProjectStatus,
          }}
          initialClips={clips.map((c) => ({
            ...c,
            status: c.status as ClipStatus,
          }))}
        />
      </div>
    </main>
  );
}
