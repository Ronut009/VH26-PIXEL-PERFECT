import { cookies } from "next/headers";
import { createServerClient } from "@supabase/ssr";

export function supabaseConfigured(): boolean {
  return Boolean(process.env.NEXT_PUBLIC_SUPABASE_URL && process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY);
}

/**
 * Server-side Supabase client bound to the request's cookie jar.
 *
 * Used by route handlers, server components and the sign-out route. Writing
 * cookies throws when called from a Server Component render, which is expected
 * and harmless: `src/proxy.ts` refreshes the session cookie on every request,
 * so a render that only reads the session does not need to write one back.
 */
export async function createClient() {
  const url = process.env.NEXT_PUBLIC_SUPABASE_URL;
  const key = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY;

  if (!url || !key) {
    throw new Error(
      "Supabase is not configured. Set NEXT_PUBLIC_SUPABASE_URL and NEXT_PUBLIC_SUPABASE_ANON_KEY in web/.env.local, then restart the dev server.",
    );
  }

  const cookieStore = await cookies();

  return createServerClient(url, key, {
    cookies: {
      getAll() {
        return cookieStore.getAll();
      },
      setAll(cookiesToSet) {
        try {
          for (const { name, value, options } of cookiesToSet) {
            cookieStore.set(name, value, options);
          }
        } catch {
          // Called from a Server Component render, where cookies are readonly.
          // The proxy already refreshed the session, so this is safe to skip.
        }
      },
    },
  });
}
