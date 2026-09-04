import { callBackend, errorResponse } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

/**
 * Ask the backend for a disposable unified diff.
 *
 * The backend builds this in a temporary local workspace and throws it away
 * before responding. There is no endpoint anywhere in PulseGraph that applies
 * it, and this route adds none.
 */
export async function POST(
  _request: Request,
  context: RouteContext<"/api/github/analyses/[analysisId]/patch-preview">,
): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  const { analysisId } = await context.params;
  if (!analysisId.trim()) {
    return errorResponse(422, "invalid_request", "An analysis ID is required.");
  }
  return callBackend({
    path: `/v1/github/analyses/${encodeURIComponent(analysisId)}/patch-preview`,
    method: "POST",
    admin: true,
  });
}
