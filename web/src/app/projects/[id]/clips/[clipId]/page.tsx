export const dynamic = "force-dynamic";

import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { ClipAudioViz } from "./ClipAudioViz";

export default async function ClipPage({
  params,
}: {
  params: Promise<{ id: string; clipId: string }>;
}) {
  const { id: projectId, clipId } = await params;

  const supabase = await createClient();
  const { data: clip, error } = await supabase
    .from("clips")
    .select("id, project_id, filename, status, duration_secs")
    .eq("id", clipId)
    .maybeSingle();

  if (error) {
    console.error("Clip fetch error:", error.message);
    throw new Error(error.message);
  }
  if (!clip || clip.project_id !== projectId) notFound();

  return (
    <main className="flex flex-col min-h-full">
      <header className="flex items-center gap-3 px-4 py-4 border-b border-zinc-200 dark:border-zinc-800">
        <Link
          href={`/projects/${projectId}`}
          className="text-zinc-500 dark:text-zinc-400 text-sm"
          aria-label="Back"
        >
          ←
        </Link>
        <h1 className="text-base font-semibold truncate font-mono">
          {clip.filename}
        </h1>
      </header>

      <div className="flex flex-col gap-4 px-4 py-6 max-w-3xl mx-auto w-full">
        <ClipAudioViz
          clipId={clip.id}
          filename={clip.filename}
          durationSecs={clip.duration_secs ?? null}
        />
      </div>
    </main>
  );
}
