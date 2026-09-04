// Mirrors src/db/schema.sql. Lifecycle values are the uppercase set the
// backend enforces via CHECK (status IN ('OPEN','ACKNOWLEDGED','QUIESCENT',
// 'RESOLVED')) — see src/db/schema.sql. The SSE stream calls this same field
// `state` and camelCases everything; normalizeIncident() below absorbs both
// shapes so components only ever see one.

export type Severity = "critical" | "high" | "medium" | "low";
export type Lifecycle = "OPEN" | "ACKNOWLEDGED" | "QUIESCENT" | "RESOLVED";
export type RouteDecision = "slack" | "pagerduty" | "email" | "suppressed" | null;

export interface Incident {
  incident_id: string;
  title: string;
  summary: string | null;
  severity: Severity;
  status: Lifecycle;
  alert_count: number;
  first_alert_at: string | null;
  last_alert_at: string | null;
  ewma_rate: number;
  route_decision: RouteDecision;
  root_cause_hint: string | null;
  quiet_at_ms: number | null;
  created_at: string | null;
  updated_at: string;
}

export interface IncidentEdge {
  src_incident_id: string;
  dst_incident_id: string;
  weight: number;
  last_seen_at: string;
}

const LIFECYCLE_ALIASES: Record<string, Lifecycle> = {
  OPEN: "OPEN",
  ACKNOWLEDGED: "ACKNOWLEDGED",
  QUIESCENT: "QUIESCENT",
  RESOLVED: "RESOLVED",
  // Pre-Phase-2 rows and the older frontend contract used these.
  new: "OPEN",
  active: "OPEN",
  acknowledged: "ACKNOWLEDGED",
  quiet: "QUIESCENT",
  resolved: "RESOLVED",
};

export function toLifecycle(value: string | null | undefined): Lifecycle {
  if (!value) return "OPEN";
  return LIFECYCLE_ALIASES[value] ?? LIFECYCLE_ALIASES[value.toUpperCase()] ?? "OPEN";
}

/** Accepts a REST row (snake_case) or an SSE incident (camelCase). */
export function normalizeIncident(raw: Record<string, unknown>): Incident {
  const pick = <T>(...keys: string[]): T | undefined => {
    for (const key of keys) {
      const value = raw[key];
      if (value !== undefined && value !== null) return value as T;
    }
    return undefined;
  };

  return {
    incident_id: pick<string>("incident_id", "incidentId") ?? "",
    title: pick<string>("title") ?? "untitled incident",
    summary: pick<string>("summary") ?? null,
    severity: (pick<string>("severity") ?? "medium").toLowerCase() as Severity,
    status: toLifecycle(pick<string>("status", "state")),
    alert_count: Number(pick<number>("alert_count", "alertCount") ?? 1),
    first_alert_at: pick<string>("first_alert_at", "firstAlertAt") ?? null,
    last_alert_at: pick<string>("last_alert_at", "lastAlertAt", "updated_at", "updatedAt") ?? null,
    ewma_rate: Number(pick<number>("ewma_rate", "ewmaRate") ?? 0),
    route_decision: (pick<string>("route_decision", "routeDecision") ?? null) as RouteDecision,
    root_cause_hint: pick<string>("root_cause_hint", "rootCauseHint") ?? null,
    quiet_at_ms: (pick<number>("quiet_at_ms", "quietAtMs") as number | undefined) ?? null,
    created_at: pick<string>("created_at", "createdAt") ?? null,
    updated_at: pick<string>("updated_at", "updatedAt") ?? new Date().toISOString(),
  };
}

/** Accepts a REST edge row or an SSE edge. */
export function normalizeEdge(raw: Record<string, unknown>): IncidentEdge {
  return {
    src_incident_id: (raw.src_incident_id ?? raw.sourceIncidentId ?? "") as string,
    dst_incident_id: (raw.dst_incident_id ?? raw.targetIncidentId ?? "") as string,
    weight: Number(raw.weight ?? raw.jointWeight ?? 1),
    last_seen_at: (raw.last_seen_at ?? raw.lastSeenAt ?? "") as string,
  };
}
