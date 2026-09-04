"use client";

import { useState } from "react";
import { createClient } from "@/lib/supabase/client";

export function SignInButton() {
  const [pending, setPending] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const signIn = async () => {
    setPending(true);
    setError(null);
    try {
      const supabase = createClient();
      const { error } = await supabase.auth.signInWithOAuth({
        provider: "github",
        options: {
          // Must also be listed under Supabase → Authentication → URL
          // Configuration, or Supabase refuses the redirect.
          redirectTo: `${window.location.origin}/auth/callback`,
        },
      });
      if (error) throw error;
      // On success the browser navigates to GitHub, so `pending` stays true.
    } catch (cause) {
      setPending(false);
      setError(cause instanceof Error ? cause.message : "Could not start GitHub sign-in.");
    }
  };

  return (
    <div>
      <button
        type="button"
        onClick={signIn}
        disabled={pending}
        className="flex w-full items-center justify-center gap-2.5 rounded-md bg-[#172033] px-4 py-2.5 text-[14px] font-medium text-white transition-colors hover:bg-[#0f1626] disabled:cursor-not-allowed disabled:opacity-60"
      >
        <svg width="18" height="18" viewBox="0 0 16 16" fill="currentColor" aria-hidden>
          <path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0 0 16 8c0-4.42-3.58-8-8-8Z" />
        </svg>
        {pending ? "Redirecting to GitHub…" : "Sign in with GitHub"}
      </button>

      {error && (
        <p className="mt-3 rounded-md border border-edge bg-[#FEF2F2] px-3 py-2 text-[12px] text-[#B91C1C]">
          {error}
        </p>
      )}
    </div>
  );
}
