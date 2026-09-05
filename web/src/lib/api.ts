/**
 * Typed client for PulseGraph's same-origin API.
 *
 * Nothing here knows the backend's address. Every call targets a narrowly
 * scoped Route Handler under `src/app/api/`, which forwards to exactly one
 * FastAPI endpoint on the server (see `src/server/pulsegraph.ts`). Two reasons
 * this indirection is not optional:
 *
 *  1. The backend sets no CORS headers, so a browser `fetch` or `EventSource`
 *     aimed at it directly is blocked. Opening it up with
 *     `Access-Control-Allow-Origin: *` is the wrong fix — that same backend
 *     also serves privileged GitHub management routes.
 *  2. `GITHUB_ADMIN_TOKEN` authorizes those GitHub routes. The route handler
 *     attaches it after the request has left the browser, so the calls below
 *     carry no credentials of their own.
 */

import { normalizeEdge, normalizeIncident } from "./types";
import type {
  ApiErrorBody,
  ApiErrorCode,
  GithubAnalysis,
  GithubInstallationSync,
  GithubPatchPreview,
  GithubRepository,
  GithubServiceMapping,
  GithubSnapshot,
  HealthReport,
  SelfHealthReport,
  Incident,
  IncidentEdge,
} from "./types";

/** A failed `/api/*` call, carrying the envelope's machine-readable code. */
export class PulseGraphApiError extends Error {
  readonly code: ApiErrorCode;
  readonly status: number;
  readonly upstreamStatus: number | undefined;

  constructor(message: string, code: ApiErrorCode, status: number, upstreamStatus?: number) {
    super(message);
    this.name = "PulseGraphApiError";
    this.code = code;
    this.status = status;
    this.upstreamStatus = upstreamStatus;
  }
}

interface RequestOptions {
  method?: "GET" | "POST" | "PUT";
  body?: unknown;
  search?: Record<string, string | number | boolean | undefined>;
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const { method = "GET", body, search } = options;

  const query = new URLSearchParams();
  for (const [key, value] of Object.entries(search ?? {})) {
    if (value !== undefined) query.set(key, String(value));
  }
  const url = query.size > 0 ? `${path}?${query.toString()}` : path;

  let res: Response;
  try {
    res = await fetch(url, {
      method,
      cache: "no-store",
      headers: body === undefined ? undefined : { "content-type": "application/json" },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    // The dashboard's own server is unreachable — a different failure from the
    // backend being down, and worth distinguishing.
    throw new PulseGraphApiError(
      "The dashboard server did not respond. Check that the Next.js server is still running.",
      "backend_unreachable",
      0,
    );
  }

  const text = await res.text();
  const parsed: unknown = text ? safeJsonParse(text) : null;

  if (!res.ok) {
    const envelope = asErrorBody(parsed);
    const code = envelope?.error.code ?? "upstream_error";

    throw new PulseGraphApiError(
      envelope?.error.message ?? `Request to ${path} failed with HTTP ${res.status}.`,
      code,
      res.status,
      envelope?.error.upstream_status,
    );
  }

  return parsed as T;
}

function safeJsonParse(text: string): unknown {
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function asErrorBody(parsed: unknown): ApiErrorBody | null {
  if (!parsed || typeof parsed !== "object" || !("error" in parsed)) return null;
  const error = (parsed as { error: unknown }).error;
  if (!error || typeof error !== "object") return null;
  const { code, message } = error as { code?: unknown; message?: unknown };
  if (typeof code !== "string" || typeof message !== "string") return null;
  return parsed as ApiErrorBody;
}

// ── Public endpoints ─────────────────────────────────────────────────────

/**
 * Liveness of the backend and its writer connection. `/v1/health` runs a real
 * `SELECT 1`, so an "unhealthy" answer means the database, not the network.
 */
export async function fetchHealth(): Promise<HealthReport> {
  return request<HealthReport>("/api/health");
}

export async function fetchSelfHealth(): Promise<SelfHealthReport> {
  return request<SelfHealthReport>("/api/health/self");
}

export async function fetchIncidentsSince(since?: string): Promise<Incident[]> {
  const body = await request<{ incidents: Record<string, unknown>[] }>("/api/incidents/recent", {
    search: { since },
  });
  return (body.incidents ?? []).map(normalizeIncident);
}

/**
 * Correlation edges for a cold load. They are also published on the SSE stream
 * (GET /v1/stream, `snapshot` + `graph.edge.upsert`), but a stream only carries
 * what happens after you subscribe - so before this route existed, a first page
 * view rendered an empty correlation graph even with edges in the database.
 *
 * Still resolves empty rather than throwing: an unreachable backend should
 * leave the panel pending, not break the page.
 */
export async function fetchIncidentEdges(): Promise<IncidentEdge[]> {
  try {
    const body = await request<{ edges?: Record<string, unknown>[] }>("/api/incidents/edges");
    return (body.edges ?? []).map(normalizeEdge);
  } catch {
    return [];
  }
}

/**
 * The same-origin SSE endpoint an `EventSource` subscribes to. `after` is the
 * broker's monotonic stream cursor, so a reconnect resumes without replaying
 * or skipping events.
 */
export function streamUrl(after: number): string {
  return after > 0 ? `/api/stream?after=${after}` : "/api/stream";
}

// ── GitHub management endpoints ──────────────────────────────────────────
// These reach FastAPI routes guarded by `Authorization: Bearer
// <GITHUB_ADMIN_TOKEN>`. The token is attached server-side, inside the route
// handler; it is never sent from, stored in, or readable by the browser.
//
// The backend is read-only by construction. Nothing below pushes, commits,
// branches, merges, opens a pull request, or edits a repository — snapshots
// and analyses are written to PulseGraph's own database, and a patch preview
// is a disposable diff for a human to read.

export async function fetchGithubInstallUrl(): Promise<string> {
  const body = await request<{ install_url: string }>("/api/github/install-url");
  return body.install_url;
}

export async function fetchGithubRepositories(): Promise<GithubRepository[]> {
  const body = await request<{ repositories: GithubRepository[] }>("/api/github/repositories");
  return body.repositories;
}

/** Re-read the selected repository list for one installation. */
export async function syncGithubInstallation(
  installationId: number,
): Promise<GithubInstallationSync> {
  return request<GithubInstallationSync>(`/api/github/installations/${installationId}/sync`, {
    method: "POST",
  });
}

/** Bind a monitored service name to one selected repository. */
export async function mapServiceToRepository(
  service: string,
  repositoryId: number,
): Promise<GithubServiceMapping> {
  return request<GithubServiceMapping>(
    `/api/github/service-mappings/${encodeURIComponent(service)}`,
    { method: "PUT", body: { repository_id: repositoryId } },
  );
}

/** Pin an immutable commit/tree inventory. Reads GitHub, writes nothing to it. */
export async function createRepositorySnapshot(repositoryId: number): Promise<GithubSnapshot> {
  return request<GithubSnapshot>(`/api/github/repositories/${repositoryId}/snapshots`, {
    method: "POST",
  });
}

export async function fetchGithubSnapshot(
  snapshotId: string,
  options: { includeFiles?: boolean; fileLimit?: number } = {},
): Promise<GithubSnapshot> {
  return request<GithubSnapshot>(`/api/github/snapshots/${encodeURIComponent(snapshotId)}`, {
    search: { include_files: options.includeFiles, file_limit: options.fileLimit },
  });
}

/**
 * Run one bounded diagnosis against the incident's pinned snapshot. A safe
 * fallback (`diagnosis.status === "fallback"`) is a successful response, not
 * an error: it is what the backend returns instead of an unverified claim.
 */
export async function createIncidentDiagnosis(incidentId: string): Promise<GithubAnalysis> {
  return request<GithubAnalysis>(
    `/api/github/incidents/${encodeURIComponent(incidentId)}/diagnoses`,
    { method: "POST" },
  );
}

export async function fetchIncidentDiagnoses(
  incidentId: string,
  limit?: number,
): Promise<GithubAnalysis[]> {
  const body = await request<{ analyses: GithubAnalysis[] }>(
    `/api/github/incidents/${encodeURIComponent(incidentId)}/diagnoses`,
    { search: { limit } },
  );
  return body.analyses;
}

export async function fetchGithubAnalysis(analysisId: string): Promise<GithubAnalysis> {
  return request<GithubAnalysis>(`/api/github/analyses/${encodeURIComponent(analysisId)}`);
}

/** Produce a human-reviewable unified diff. Nothing in PulseGraph applies it. */
export async function createPatchPreview(analysisId: string): Promise<GithubPatchPreview> {
  return request<GithubPatchPreview>(
    `/api/github/analyses/${encodeURIComponent(analysisId)}/patch-preview`,
    { method: "POST" },
  );
}
