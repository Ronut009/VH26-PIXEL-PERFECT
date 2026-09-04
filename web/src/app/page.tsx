import { redirect } from "next/navigation";
import { DashboardApp } from "@/components/DashboardApp";
import { getAuthState } from "@/server/auth";

export const dynamic = "force-dynamic";

/**
 * The dashboard route.
 *
 * `src/proxy.ts` already redirects signed-out visitors here, but this check is
 * not redundant: the Next docs warn that a proxy matcher change can silently
 * drop coverage, so the route that actually renders incident data verifies the
 * session itself.
 */
export default async function Home() {
  const state = await getAuthState();

  if (state.status !== "ok") {
    // /login renders the reason: signed out, not on the allowlist, or Supabase
    // not configured yet.
    redirect("/login");
  }

  return <DashboardApp user={state.user} />;
}
