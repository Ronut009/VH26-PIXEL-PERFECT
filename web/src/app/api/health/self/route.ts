import { callBackend } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

/**
 * The self-check report behind `GET /v1/health/self`.
 *
 * Unlike `/api/health`, this one requires a session: it carries worker
 * liveness, outbox depth, circuit-breaker state and clock skew — operational
 * detail about the deployment rather than a bare liveness bit.
 */
export async function GET(): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  return callBackend({ path: "/v1/health/self" });
}
