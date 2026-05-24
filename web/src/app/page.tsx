export const dynamic = "force-dynamic";

import Link from "next/link";
import { createClient } from "@/lib/supabase/server";
import { NewProjectForm } from "./NewProjectForm";

function statusLabel(status: string) {
  switch (status) {
    case "uploading":
      return "Uploading";
    case "transcribing":
      return "Transcribing";
    case "transcribed":
      return "Ready";
    case "rendering":
      return "Rendering";
    case "done":
      return "Done";
    case "error":
      return "Error";
    default:
      return status;
  }
}

function statusColor(status: string) {
  switch (status) {
    case "done":
      return "text-green-600 dark:text-green-400";
    case "error":
      return "text-red-500";
    case "transcribed":
      return "text-blue-600 dark:text-blue-400";
    default:
      return "text-zinc-500 dark:text-zinc-400";
  }
}

export default async function Home() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  const { data: projects } = await supabase
    .from("projects")
    .select("id, name, status, created_at")
    .order("created_at", { ascending: false });

  return (
    <main className="flex flex-col min-h-full">
      {/* Header */}
      <header className="flex items-center justify-between px-4 py-4 border-b border-zinc-200 dark:border-zinc-800">
        <h1 className="text-lg font-semibold tracking-tight">lyricsync</h1>
        {user && (
          <form action="/auth/signout" method="post">
            <button type="submit" className="text-xs text-zinc-500 dark:text-zinc-400">
              Sign out
            </button>
          </form>
        )}
      </header>

      {/* Content */}
      <div className="flex flex-col gap-4 px-4 py-6 max-w-lg mx-auto w-full">
        <NewProjectForm />

        {projects && projects.length > 0 ? (
          <ul className="flex flex-col gap-2">
            {projects.map((project) => (
              <li key={project.id}>
                <Link
                  href={`/projects/${project.id}`}
                  className="flex items-center justify-between rounded-xl border border-zinc-200 bg-white px-4 py-3 dark:border-zinc-800 dark:bg-zinc-900"
                >
                  <span className="text-sm font-medium truncate pr-2">
                    {project.name}
                  </span>
                  <span className={`shrink-0 text-xs ${statusColor(project.status)}`}>
                    {statusLabel(project.status)}
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        ) : (
          <p className="text-center text-sm text-zinc-500 dark:text-zinc-400 py-8">
            No projects yet. Create one to get started.
          </p>
        )}
      </div>
    </main>
  );
}
