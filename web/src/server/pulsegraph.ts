/**
 * Server-only client for the FastAPI backend.
 *
 * The browser never calls http://127.0.0.1:8000 directly. Two reasons, and
 * both of them are hard requirements rather than preferences:
 *
 *  1. The backend enables no CORS, and the fix is *not* to open it up with
 *     `Access-Control-Allow-Origin: *` — that would make a read-only-but-
 *     privileged API reachable from any page the operator happens to have
 *     open. Instead every dashboard request goes to a same-origin
 *     `/api/*` route handler in this app, which is the only caller of this
 *     module.
 *  2. `GITHUB_ADMIN_TOKEN` authorizes the GitHub management endpoints. It is
 *     read here, on the server, and attached to the upstream request. It is
 *     deliberately not `NEXT_PUBLIC_`, is never echoed into a response body,
 *     and never reaches a client component.
 */

import type { ApiErrorCode } from "@/lib/types";

if (typeof window !== "undefined") {
  // A stray import from a client component would otherwise fail silently
  // (with `process.env.GITHUB_ADMIN_TOKEN` simply undefined in the bundle).
  // Failing loudly keeps the token boundary a build/runtime error, not a
  // subtle "why is auth broken" bug.
  throw new Error("@/server/pulsegraph is server-only and must not be imported by client code");
}

const DEFAULT_API_BASE = "http://127.0.0.1:8000";
const REQUEST_TIMEOUT_MS = 15_000;

/**
 * Machine-readable failure codes the UI switches on.
 *
 * Declared once in `@/lib/types` (which the browser may import) and re-exported
 * here, so the server and client unions cannot drift apart.
 */
export type { ApiErrorCode };

export interface ApiErrorPayload {
  error: {
    code: ApiErrorCode;
    message: string;
    /** Present when the backend answered and its status is worth showing. */
    upstream_status?: number;
  };
}

/**
 * The backend origin, read on the server only. `NEXT_PUBLIC_API_BASE` is
 * deliberately not consulted: the browser no longer needs — or gets — the
 * backend's address at all.
 */
function apiBase(): string {
  const configured = process.env.PULSEGRAPH_API_BASE;
  return configured && configured.trim() ? configured.trim() : DEFAULT_API_BASE;
}

export function errorResponse(
  status: number,
  code: ApiErrorCode,
  message: string,
  upstreamStatus?: number,
): Response {
  const payload: ApiErrorPayload = {
    error: upstreamStatus === undefined ? { code, message } : { code, message, upstream_status: upstreamStatus },
  };
  return Response.json(payload, { status });
}

export type QueryValue = string | number | boolean | undefined | null;

export interface BackendRequest {
  /** Path on the backend, already percent-encoded, e.g. `/v1/github/repositories`. */
  path: string;
  method?: "GET" | "POST" | "PUT";
  query?: Record<string, QueryValue>;
  /** JSON request body. Omitted entirely when undefined. */
  body?: unknown;
  /**
   * Whether this endpoint sits behind `Authorization: Bearer
   * <GITHUB_ADMIN_TOKEN>`. Public endpoints must not send it.
   */
  admin?: boolean;
}

/**
 * Call the backend and translate the outcome into a response this app is
 * happy to hand to the browser.
 *
 * Errors are normalized to `{ error: { code, message, upstream_status } }` so
 * the client has something typed to branch on. FastAPI's `detail` strings are
 * PulseGraph's own copy (no credentials, no provider internals — see
 * `src/github_integration/router.py`), so passing them through is useful
 * rather than leaky.
 */
export async function callBackend({
  path,
  method = "GET",
  query,
  body,
  admin = false,
}: BackendRequest): Promise<Response> {
  const url = new URL(path, apiBase());
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }

  const headers = new Headers({ accept: "application/json" });

  if (admin) {
    const token = process.env.GITHUB_ADMIN_TOKEN;
    if (!token) {
      return errorResponse(
        503,
        "admin_token_missing",
        "GITHUB_ADMIN_TOKEN is not set for the dashboard server. Add it to web/.env.local (server-only, no NEXT_PUBLIC_ prefix) and restart.",
      );
    }
    headers.set("authorization", `Bearer ${token}`);
  }

  let init: RequestInit = { method, headers, cache: "no-store", signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS) };
  if (body !== undefined) {
    headers.set("content-type", "application/json");
    init = { ...init, body: JSON.stringify(body) };
  }

  let upstream: globalThis.Response;
  try {
    upstream = await fetch(url, init);
  } catch {
    // Deliberately not surfacing the cause: it can contain the backend host
    // and internal network detail that the browser has no business seeing.
    return errorResponse(
      502,
      "backend_unreachable",
      "The PulseGraph backend did not respond. Start it with `uvicorn src.main:app --reload` and check PULSEGRAPH_API_BASE.",
    );
  }

  const text = await upstream.text();
  let parsed: unknown = null;
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      return errorResponse(
        502,
        "invalid_upstream_response",
        "The backend returned a response that was not JSON.",
        upstream.status,
      );
    }
  }

  if (!upstream.ok) {
    return errorResponse(upstream.status, "upstream_error", detailOf(parsed, upstream.status), upstream.status);
  }

  return Response.json(parsed, { status: upstream.status });
}

/** Pull FastAPI's `{"detail": ...}` out of an error body, with a sane fallback. */
function detailOf(parsed: unknown, status: number): string {
  if (parsed && typeof parsed === "object" && "detail" in parsed) {
    const detail = (parsed as { detail: unknown }).detail;
    if (typeof detail === "string" && detail.trim()) return detail;
    // 422 bodies are a list of validation objects; a summary beats raw JSON.
    if (Array.isArray(detail)) return "The backend rejected the request as invalid.";
  }
  return `The backend responded with HTTP ${status}.`;
}

/** Parse a JSON request body, rejecting anything that is not an object. */
export async function readJsonObject(request: Request): Promise<Record<string, unknown> | null> {
  try {
    const parsed: unknown = await request.json();
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    return parsed as Record<string, unknown>;
  } catch {
    return null;
  }
}

/** Positive-integer path/body parameter guard, so junk never reaches upstream. */
export function positiveInteger(value: string | number | undefined): number | null {
  if (value === undefined) return null;
  const parsed = typeof value === "number" ? value : Number(value);
  if (!Number.isSafeInteger(parsed) || parsed <= 0) return null;
  return parsed;
}

/**
 * Proxy a Server-Sent Events stream.
 *
 * Separate from `callBackend` because the two need opposite things: a JSON
 * call gets a timeout and a buffered body, while a stream must never time out
 * and must hand the upstream body straight through. The caller's signal is
 * forwarded so that when the browser closes the EventSource, the upstream
 * connection is torn down too instead of leaking a reader.
 */
export async function streamBackend({
  path,
  query,
  signal,
}: {
  path: string;
  query?: Record<string, QueryValue>;
  signal: AbortSignal;
}): Promise<Response> {
  const url = new URL(path, apiBase());
  for (const [key, value] of Object.entries(query ?? {})) {
    if (value !== undefined && value !== null) url.searchParams.set(key, String(value));
  }

  let upstream: globalThis.Response;
  try {
    upstream = await fetch(url, {
      headers: { accept: "text/event-stream" },
      cache: "no-store",
      signal,
    });
  } catch {
    return errorResponse(
      502,
      "backend_unreachable",
      "The PulseGraph event stream is unreachable. Start the backend and check PULSEGRAPH_API_BASE.",
    );
  }

  if (!upstream.ok || !upstream.body) {
    return errorResponse(
      upstream.status === 200 ? 502 : upstream.status,
      "upstream_error",
      `The backend event stream responded with HTTP ${upstream.status}.`,
      upstream.status,
    );
  }

  return new Response(upstream.body, {
    status: 200,
    headers: {
      "content-type": "text/event-stream; charset=utf-8",
      // `no-transform` matters behind a proxy that would otherwise buffer or
      // compress the stream and delay every event.
      "cache-control": "no-cache, no-transform",
      connection: "keep-alive",
      "x-accel-buffering": "no",
    },
  });
}
