import { callBackend } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

/**
 * Same-origin mirror of `GET /v1/edges/recent`.
 *
 * The backend route now exists, so this returns real correlation edges rather
 * than a tolerated 404. It matters on a cold load: edges used to be published
 * only on the SSE stream, so a first page view - or a reconnect after sign-in -
 * rendered an empty graph while the correlations sat in the database.
 *
 * `fetchIncidentEdges` still fails quiet, which stays useful: the panel should
 * degrade to its pending state if the backend is unreachable, not error.
 */
export async function GET(): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  return callBackend({ path: "/v1/edges/recent" });
}
