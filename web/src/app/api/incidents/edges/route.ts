import { callBackend } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

/**
 * Same-origin mirror of `GET /v1/edges/recent`.
 *
 * That backend route does not exist yet (Anish's CoOccurrenceGraph slice).
 * Proxying it anyway keeps the last browser → backend call same-origin and
 * preserves the existing behaviour exactly: the upstream 404 arrives here as
 * a normal error response, and `fetchIncidentEdges` still fails quiet so the
 * root-cause panel shows its pending state rather than an error.
 */
export async function GET(): Promise<Response> {
  return callBackend({ path: "/v1/edges/recent" });
}
