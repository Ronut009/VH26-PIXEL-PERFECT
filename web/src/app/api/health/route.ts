import { callBackend } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

export async function GET(): Promise<Response> {
  return callBackend({ path: "/v1/health" });
}
