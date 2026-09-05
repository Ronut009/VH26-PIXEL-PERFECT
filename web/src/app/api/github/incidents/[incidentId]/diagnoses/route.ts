import type { NextRequest } from "next/server";
import { LOCAL_MODEL_TIMEOUT_MS, callBackend, errorResponse } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";
// A local model reading pinned source outlives the platform's default handler
// budget; without this the route is killed before the backend ever answers.
export const maxDuration = 180;

/** List the saved analyses for one incident, newest first. */
export async function GET(
  request: NextRequest,
  context: RouteContext<"/api/github/incidents/[incidentId]/diagnoses">,
): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  const { incidentId } = await context.params;
  if (!incidentId.trim()) {
    return errorResponse(422, "invalid_request", "An incident ID is required.");
  }

  const limit = Number(request.nextUrl.searchParams.get("limit"));

  return callBackend({
    path: `/v1/github/incidents/${encodeURIComponent(incidentId)}/diagnoses`,
    query: { limit: Number.isSafeInteger(limit) && limit > 0 ? Math.min(limit, 100) : undefined },
    admin: true,
  });
}

/**
 * Run one bounded diagnosis against the incident's pinned snapshot.
 *
 * The backend may legitimately answer with a safe fallback when source or
 * model access is unavailable; that is a successful response, not an error.
 */
export async function POST(
  _request: Request,
  context: RouteContext<"/api/github/incidents/[incidentId]/diagnoses">,
): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  const { incidentId } = await context.params;
  if (!incidentId.trim()) {
    return errorResponse(422, "invalid_request", "An incident ID is required.");
  }
  return callBackend({
    path: `/v1/github/incidents/${encodeURIComponent(incidentId)}/diagnoses`,
    method: "POST",
    admin: true,
    timeoutMs: LOCAL_MODEL_TIMEOUT_MS,
  });
}
