"use client";

import { useFormStatus } from "react-dom";
import { sendMagicLink } from "./actions";

const ERROR_MESSAGES: Record<string, string> = {
  auth: "That sign-in link was invalid or expired. Try again.",
  unauthorized: "This account is not allowed to access lyricsync.",
};

function SubmitButton() {
  const { pending } = useFormStatus();
  return (
    <button
      type="submit"
      disabled={pending}
      className="rounded-lg bg-black px-3 py-2 text-base font-medium text-white disabled:opacity-50 dark:bg-white dark:text-black"
    >
      {pending ? "Sending…" : "Send magic link"}
    </button>
  );
}

export default function LoginForm({
  errorCode,
}: {
  errorCode: string | null;
}) {
  const errorMsg = errorCode ? (ERROR_MESSAGES[errorCode] ?? "Something went wrong.") : null;

  return (
    <form action={sendMagicLink} className="flex w-full max-w-xs flex-col gap-3">
      <label htmlFor="email" className="text-sm font-medium">
        Email
      </label>
      <input
        id="email"
        name="email"
        type="email"
        inputMode="email"
        autoComplete="email"
        required
        placeholder="you@example.com"
        className="rounded-lg border border-zinc-300 px-3 py-2 text-base outline-none focus:border-zinc-500 dark:border-zinc-700 dark:bg-zinc-900"
      />
      <SubmitButton />
      {errorMsg && (
        <p className="text-sm text-red-600 dark:text-red-400">{errorMsg}</p>
      )}
    </form>
  );
}
