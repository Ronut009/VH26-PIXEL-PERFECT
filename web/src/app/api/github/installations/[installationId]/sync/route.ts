import { callBackend, errorResponse, positiveInteger } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

/** Re-read the selected repository list for one installation. */
export async function POST(
  _request: Request,
  context: RouteContext<"/api/github/installations/[installationId]/sync">,
): Promise<Response> {
  const { installationId } = await context.params;
  const id = positiveInteger(installationId);
  if (id === null) {
    return errorResponse(422, "invalid_request", "installationId must be a positive integer.");
  }
  return callBackend({ path: `/v1/github/installations/${id}/sync`, method: "POST", admin: true });
}
