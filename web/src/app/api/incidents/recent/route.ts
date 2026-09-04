import type { NextRequest } from "next/server";
import { callBackend } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

/**
 * Same-origin mirror of `GET /v1/incidents/recent`. The dashboard polls this
 * instead of the backend directly, so no CORS relaxation is needed on a
 * backend that also serves privileged GitHub routes.
 */
export async function GET(request: NextRequest): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  const since = request.nextUrl.searchParams.get("since");
  return callBackend({ path: "/v1/incidents/recent", query: { since } });
}
