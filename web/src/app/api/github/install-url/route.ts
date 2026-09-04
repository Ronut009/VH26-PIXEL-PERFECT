import { callBackend } from "@/server/pulsegraph";
import { requireDashboardUser } from "@/server/auth";

export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  const denied = await requireDashboardUser();
  if (denied) return denied;

  return callBackend({ path: "/v1/github/install-url", admin: true });
}
