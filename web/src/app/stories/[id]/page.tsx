export const dynamic = "force-dynamic";

import { redirect } from "next/navigation";
import { createClient } from "@/lib/supabase/server";
import { StoryViewer } from "./StoryViewer";
import { LibrarySearch } from "./LibrarySearch";

interface Props {
  params: Promise<{ id: string }>;
}

export default async function StoryPage({ params }: Props) {
  const { id: storyId } = await params;

  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  if (!user?.email) {
    redirect("/login");
  }

  const { data: story } = await supabase
    .from("stories")
    .select("id, project_id, status, error_message, render_r2_key, created_at")
    .eq("id", storyId)
    .maybeSingle();

  if (!story) {
    return (
      <main className="min-h-screen flex items-center justify-center">
        <p className="text-zinc-500">Story not found.</p>
      </main>
    );
  }

  return (
    <main className="min-h-screen bg-zinc-50 dark:bg-zinc-950">
      <div className="mx-auto max-w-2xl px-4 py-10 flex flex-col gap-6">
        <div className="flex items-center gap-3">
          <a
            href={`/projects/${story.project_id}`}
            className="text-sm text-zinc-400 dark:text-zinc-500 hover:text-zinc-700 dark:hover:text-zinc-200 transition-colors"
          >
            ← Back to project
          </a>
        </div>

        <h1 className="text-2xl font-bold text-zinc-900 dark:text-zinc-100">
          Cut
        </h1>

        <StoryViewer storyId={storyId} initialStory={story} />

        <LibrarySearch storyId={storyId} />
      </div>
    </main>
  );
}
