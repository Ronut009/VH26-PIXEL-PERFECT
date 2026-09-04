import type { NextRequest } from "next/server";
import { streamBackend } from "@/server/pulsegraph";

export const dynamic = "force-dynamic";

/**
 * Same-origin mirror of `GET /v1/stream`.
 *
 * `EventSource` cannot send credentials or custom headers cross-origin, and
 * the backend sets no CORS headers, so the browser subscribes here instead.
 * The `after` cursor is the monotonic stream ID the broker assigns
 * (`src/stream/sse_broker.py`), forwarded so a reconnect resumes exactly
 * where it left off.
 */
export async function GET(request: NextRequest): Promise<Response> {
  const after = Number(request.nextUrl.searchParams.get("after"));
  return streamBackend({
    path: "/v1/stream",
    query: { after: Number.isSafeInteger(after) && after > 0 ? after : undefined },
    signal: request.signal,
  });
}
