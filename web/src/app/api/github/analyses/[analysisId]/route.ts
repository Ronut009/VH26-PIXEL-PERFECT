import { callBackend, errorResponse } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

export async function GET(
  _request: Request,
  context: RouteContext<"/api/github/analyses/[analysisId]">,
): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  const { analysisId } = await context.params;
  if (!analysisId.trim()) {
    return errorResponse(422, "invalid_request", "An analysis ID is required.");
  }
  return callBackend({ path: `/v1/github/analyses/${encodeURIComponent(analysisId)}`, admin: true });
}
