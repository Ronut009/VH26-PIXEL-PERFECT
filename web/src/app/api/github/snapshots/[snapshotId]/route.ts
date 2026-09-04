import type { NextRequest } from "next/server";
import { callBackend, errorResponse } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

export async function GET(
  request: NextRequest,
  context: RouteContext<"/api/github/snapshots/[snapshotId]">,
): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  const { snapshotId } = await context.params;
  if (!snapshotId.trim()) {
    return errorResponse(422, "invalid_request", "A snapshot ID is required.");
  }

  const params = request.nextUrl.searchParams;
  const fileLimit = Number(params.get("file_limit"));

  return callBackend({
    path: `/v1/github/snapshots/${encodeURIComponent(snapshotId)}`,
    query: {
      include_files: params.get("include_files") === "true",
      file_limit: Number.isSafeInteger(fileLimit) && fileLimit > 0 ? Math.min(fileLimit, 1000) : undefined,
    },
    admin: true,
  });
}
