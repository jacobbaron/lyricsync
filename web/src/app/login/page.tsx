import LoginForm from "./LoginForm";

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<Record<string, string>>;
}) {
  const params = await searchParams;
  const sent = params.sent === "1";
  const errorCode = params.error ?? null;

  return (
    <main className="flex flex-1 flex-col items-center justify-center gap-6 px-6 py-16">
      <h1 className="text-2xl font-semibold tracking-tight">lyricsync</h1>

      {sent ? (
        <p className="max-w-xs text-center text-sm text-zinc-600 dark:text-zinc-400">
          Check your email for a sign-in link. You can close this tab.
        </p>
      ) : (
        <LoginForm errorCode={errorCode} />
      )}
    </main>
  );
}
