import { callBackend } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

/**
 * Liveness probe. Deliberately NOT behind `requireDashboardUser()`.
 *
 * It proxies `/v1/health`, whose entire body is `{"status":"healthy"}` plus a
 * failure string — no incident data, no configuration, nothing about the
 * GitHub installation. Requiring a session here would only break the tooling
 * that needs it most: `scripts/demo.py` checks this endpoint to tell "the
 * dashboard cannot reach the backend" apart from "the backend is down", and a
 * 401 makes those two indistinguishable.
 *
 * Every route that returns real data still requires a session.
 */
export async function GET(): Promise<Response> {
  return callBackend({ path: "/v1/health" });
}
