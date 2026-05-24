import { createClient } from "@/lib/supabase/server";

export default async function Home() {
  const supabase = await createClient();
  const {
    data: { user },
  } = await supabase.auth.getUser();

  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-4 px-6 py-16 text-center">
      <h1 className="text-2xl font-semibold tracking-tight">lyricsync</h1>
      <p className="max-w-xs text-sm text-zinc-600 dark:text-zinc-400">
        Upload phone videos, transcribe them, pick the moments you want, and
        render a cut — all from your phone.
      </p>
      {user && (
        <form action="/auth/signout" method="post" className="mt-2">
          <p className="mb-2 text-xs text-zinc-500">
            Signed in as {user.email}
          </p>
          <button
            type="submit"
            className="rounded-lg border border-zinc-300 px-3 py-1.5 text-sm dark:border-zinc-700"
          >
            Sign out
          </button>
        </form>
      )}
    </main>
  );
}
