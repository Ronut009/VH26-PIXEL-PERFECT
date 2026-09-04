import { normalizeEdge, normalizeIncident } from "./types";
import type { Incident, IncidentEdge } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export async function fetchIncidentsSince(since?: string): Promise<Incident[]> {
  const url = new URL("/v1/incidents/recent", API_BASE);
  if (since) url.searchParams.set("since", since);

  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`incidents fetch failed: ${res.status}`);

  const body = (await res.json()) as { incidents: Record<string, unknown>[] };
  return (body.incidents ?? []).map(normalizeIncident);
}

// There is no REST edges route on the backend; correlation edges are only
// published on the SSE stream (GET /v1/stream, `snapshot` + `graph.edge.upsert`).
// This stays isolated so switching this one function over to the stream does
// not touch any component. Until then it resolves empty rather than throwing.
export async function fetchIncidentEdges(): Promise<IncidentEdge[]> {
  try {
    const url = new URL("/v1/edges/recent", API_BASE);
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return [];
    const body = (await res.json()) as { edges?: Record<string, unknown>[] };
    return (body.edges ?? []).map(normalizeEdge);
  } catch {
    return [];
  }
}
