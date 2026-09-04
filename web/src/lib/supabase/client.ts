import { createBrowserClient } from "@supabase/ssr";

/**
 * Browser-side Supabase client.
 *
 * Only the sign-in button needs this: it starts the GitHub OAuth redirect.
 * Everything else reads the session on the server, so no dashboard data is
 * ever fetched with this client.
 *
 * Both values are public by design. The anon/publishable key identifies the
 * project and carries no privileges on its own; access is decided by the
 * session cookie and, in this app, by the server-side allowlist.
 */
export function createClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !key) {
    throw new Error(
      "Supabase is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY in web/.env.local, then restart the dev server.",
    );
  }

  return createBrowserClient(url, key);
}
