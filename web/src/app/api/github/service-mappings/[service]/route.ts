import { callBackend, errorResponse, positiveInteger, readJsonObject } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

/** Bind a monitored service name to one selected repository. */
export async function PUT(
  request: Request,
  context: RouteContext<"/api/github/service-mappings/[service]">,
): Promise<Response> {
  const { service } = await context.params;
  if (!service.trim()) {
    return errorResponse(422, "invalid_request", "A service name is required.");
  }

  const body = await readJsonObject(request);
  const repositoryId = positiveInteger(
    typeof body?.repository_id === "number" || typeof body?.repository_id === "string"
      ? body.repository_id
      : undefined,
  );
  if (repositoryId === null) {
    return errorResponse(422, "invalid_request", "repository_id must be a positive integer.");
  }

  return callBackend({
    path: `/v1/github/service-mappings/${encodeURIComponent(service)}`,
    method: "PUT",
    body: { repository_id: repositoryId },
    admin: true,
  });
}
