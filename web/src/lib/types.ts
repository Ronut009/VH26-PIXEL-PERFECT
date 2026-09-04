// Mirrors src/db/schema.sql (incidents, edges) and src/contracts.py on the
// Python side. Keep these in sync with the backend by hand until there's a
// shared schema generator — small enough surface for now.

export type Severity = "critical" | "high" | "medium" | "low";
export type IncidentStatus = "new" | "active" | "quiet" | "resolved";
export type RouteDecision = "slack" | "pagerduty" | "email" | "suppressed" | null;

export interface Incident {
  incident_id: string;
  title: string;
  summary: string | null;
  severity: Severity;
  status: IncidentStatus;
  alert_count: number;
  first_alert_at: string;
  last_alert_at: string;
  ewma_rate: number;
  route_decision: RouteDecision;
  root_cause_hint: string | null;
  created_at: string;
  updated_at: string;
}

export interface IncidentEdge {
  src_incident_id: string;
  dst_incident_id: string;
  weight: number;
  last_seen_at: string;
}
