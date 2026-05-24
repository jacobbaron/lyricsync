export const dynamic = "force-dynamic";

import Link from "next/link";
import { notFound } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { UploadArea } from "./UploadArea";

type ClipStatus =
  | "uploading"
  | "uploading_complete"
  | "transcribing"
  | "aligned"
  | "error";

function clipStatusLabel(status: ClipStatus) {
  switch (status) {
    case "uploading":
      return "Uploading";
    case "uploading_complete":
      return "Queued";
    case "transcribing":
      return "Transcribing";
    case "aligned":
      return "Done";
    case "error":
      return "Error";
    default:
      return status;
  }
}

function clipStatusColor(status: ClipStatus) {
  switch (status) {
    case "aligned":
      return "text-green-600 dark:text-green-400";
    case "error":
      return "text-red-500";
    case "transcribing":
      return "text-blue-500";
    default:
      return "text-zinc-500 dark:text-zinc-400";
  }
}

function projectStatusBanner(status: string) {
  switch (status) {
    case "uploading":
      return { text: "Waiting for uploads to finish", color: "text-zinc-500" };
    case "transcribing":
      return { text: "Transcribing clips…", color: "text-blue-500" };
    case "transcribed":
      return {
        text: "Transcription complete — ready to pick ranges",
        color: "text-green-600 dark:text-green-400",
      };
    case "rendering":
      return { text: "Rendering cut…", color: "text-blue-500" };
    case "done":
      return { text: "Done", color: "text-green-600 dark:text-green-400" };
    case "error":
      return { text: "An error occurred", color: "text-red-500" };
    default:
      return null;
  }
}

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

  if (error || !project) notFound();

  const clips = (project.clips ?? []) as Array<{
    id: string;
    filename: string;
    status: ClipStatus;
    error_message: string | null;
  }>;

  const banner = projectStatusBanner(project.status);

  return (
    <main className="flex flex-col min-h-full">
      {/* Header */}
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
        {/* Status banner */}
        {banner && (
          <p className={`text-sm ${banner.color}`}>{banner.text}</p>
        )}

        {/* Existing clips from DB */}
        {clips.length > 0 && (
          <section>
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 mb-2">
              Clips
            </h2>
            <ul className="flex flex-col gap-2">
              {clips.map((clip) => (
                <li
                  key={clip.id}
                  className="flex flex-col gap-1 rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="text-sm truncate text-zinc-800 dark:text-zinc-200">
                      {clip.filename}
                    </span>
                    <span
                      className={`shrink-0 text-xs ${clipStatusColor(clip.status)}`}
                    >
                      {clipStatusLabel(clip.status)}
                    </span>
                  </div>
                  {clip.status === "error" && clip.error_message && (
                    <p className="text-xs text-red-500">{clip.error_message}</p>
                  )}
                </li>
              ))}
            </ul>
          </section>
        )}

        {/* Upload area */}
        <section>
          {clips.length > 0 && (
            <h2 className="text-xs font-medium uppercase tracking-wide text-zinc-400 dark:text-zinc-500 mb-2">
              Add More
            </h2>
          )}
          <UploadArea projectId={projectId} />
        </section>
      </div>
    </main>
  );
}
