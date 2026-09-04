import type { Incident, IncidentEdge } from "./types";

const API_BASE = process.env.NEXT_PUBLIC_API_BASE ?? "http://localhost:8000";

export async function fetchIncidentsSince(since?: string): Promise<Incident[]> {
  const url = new URL("/v1/incidents/recent", API_BASE);
  if (since) url.searchParams.set("since", since);

  const res = await fetch(url, { cache: "no-store" });
  if (!res.ok) throw new Error(`incidents fetch failed: ${res.status}`);

  const body = (await res.json()) as { incidents: Incident[] };
  return body.incidents;
}

// The co-occurrence graph endpoint doesn't exist yet (Anish's slice, still
// in progress) — this stays isolated so wiring in the real route is a
// one-line change here, not a hunt through every component that draws the
// graph. Until then it fails quiet and the graph panel shows its pending
// state.
export async function fetchIncidentEdges(): Promise<IncidentEdge[]> {
  try {
    const url = new URL("/v1/edges/recent", API_BASE);
    const res = await fetch(url, { cache: "no-store" });
    if (!res.ok) return [];
    const body = (await res.json()) as { edges: IncidentEdge[] };
    return body.edges ?? [];
  } catch {
    return [];
  }
}
