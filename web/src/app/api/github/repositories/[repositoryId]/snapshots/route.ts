import { callBackend, errorResponse, positiveInteger } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

/**
 * Pin an immutable commit/tree/blob inventory for a repository.
 *
 * This reads GitHub through the read-only App installation and writes only to
 * PulseGraph's own database. Nothing is pushed, committed, or branched.
 */
export async function POST(
  _request: Request,
  context: RouteContext<"/api/github/repositories/[repositoryId]/snapshots">,
): Promise<Response> {
  const { repositoryId } = await context.params;
  const id = positiveInteger(repositoryId);
  if (id === null) {
    return errorResponse(422, "invalid_request", "repositoryId must be a positive integer.");
  }
  return callBackend({ path: `/v1/github/repositories/${id}/snapshots`, method: "POST", admin: true });
}
