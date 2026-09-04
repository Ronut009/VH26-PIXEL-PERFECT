import type { Incident, Severity, RouteDecision } from "./types";

/**
 * Every figure on the dashboard is derived here from fields the backend
 * actually returns (alert_count, severity, status, route_decision, ewma_rate,
 * timestamps). Nothing is invented. If a number cannot be computed from real
 * incident data it does not appear on screen.
 */

export function totals(incidents: Incident[]) {
  const alertsIn = incidents.reduce((sum, i) => sum + i.alert_count, 0);
  const surfaced = incidents.length;
  const paged = incidents.filter((i) => i.route_decision === "pagerduty").length;
  const silenced = incidents.filter((i) => i.route_decision === "suppressed").length;
  const reduction = alertsIn > 0 ? ((alertsIn - surfaced) / alertsIn) * 100 : 0;
  const firing = incidents.filter((i) => i.ewma_rate > 0).length;
  return { alertsIn, surfaced, paged, silenced, reduction, firing };
}

export const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low"];

/** Incidents grouped by severity, with the alert volume each band absorbed. */
export function severityMix(incidents: Incident[]) {
  return SEVERITY_ORDER.map((severity) => {
    const rows = incidents.filter((i) => i.severity === severity);
    return {
      severity,
      count: rows.length,
      alerts: rows.reduce((sum, i) => sum + i.alert_count, 0),
    };
  }).filter((row) => row.count > 0);
}

const ROUTES: RouteDecision[] = ["pagerduty", "slack", "email", "suppressed"];

/** Where the surfaced incidents were actually delivered. */
export function routingMix(incidents: Incident[]) {
  return ROUTES.map((route) => {
    const rows = incidents.filter((i) => i.route_decision === route);
    return {
      route,
      count: rows.length,
      alerts: rows.reduce((sum, i) => sum + i.alert_count, 0),
    };
  }).filter((row) => row.count > 0);
}

/** The incidents that absorbed the most duplicate alerts. */
export function noisiest(incidents: Incident[], limit = 5) {
  return [...incidents].sort((a, b) => b.alert_count - a.alert_count).slice(0, limit);
}

/**
 * Alert volume over time, bucketed by when each incident started. This is a
 * genuine distribution from first_alert_at, not a synthetic series.
 */
export function volumeByHour(incidents: Incident[], buckets = 8) {
  const stamped = incidents
    .filter((i) => i.first_alert_at)
    .map((i) => ({ at: new Date(i.first_alert_at as string).getTime(), alerts: i.alert_count }))
    .filter((row) => Number.isFinite(row.at));

  if (stamped.length === 0) return [];

  const min = Math.min(...stamped.map((r) => r.at));
  const max = Math.max(...stamped.map((r) => r.at));
  const span = Math.max(1, max - min);
  const size = span / buckets;

  const out = Array.from({ length: buckets }, (_, index) => ({
    index,
    at: min + index * size,
    alerts: 0,
  }));
  for (const row of stamped) {
    const index = Math.min(buckets - 1, Math.floor((row.at - min) / size));
    out[index].alerts += row.alerts;
  }
  return out;
}


/**
 * Ingested vs consolidated over time. Ingested is the raw alert volume that
 * arrived in each window; consolidated is the number of incidents those alerts
 * were collapsed into. Both come from real incident records.
 */
export function consolidationSeries(incidents: Incident[], buckets = 12) {
  const stamped = incidents
    .filter((i) => i.first_alert_at)
    .map((i) => ({ at: new Date(i.first_alert_at as string).getTime(), alerts: i.alert_count }))
    .filter((r) => Number.isFinite(r.at));
  if (stamped.length === 0) return [];

  const min = Math.min(...stamped.map((r) => r.at));
  const max = Math.max(...stamped.map((r) => r.at));
  const size = Math.max(1, max - min) / buckets;

  const out = Array.from({ length: buckets }, (_, index) => ({
    index,
    at: min + index * size,
    ingested: 0,
    consolidated: 0,
  }));
  for (const row of stamped) {
    const i = Math.min(buckets - 1, Math.floor((row.at - min) / size));
    out[i].ingested += row.alerts;
    out[i].consolidated += 1;
  }
  return out;
}

/**
 * The adaptive quiet window, read from the engine's quiet_at_ms deadline.
 * Returns null when no incident currently carries a deadline, rather than
 * inventing one.
 */
export function quietWindow(incidents: Incident[]) {
  const deadlines = incidents
    .map((i) => i.quiet_at_ms)
    .filter((v): v is number => typeof v === "number" && v > 0);
  if (deadlines.length === 0) return null;

  const next = Math.max(...deadlines);
  const active = incidents.filter((i) => i.status !== "RESOLVED");
  const absorbed = active.reduce((sum, i) => sum + Math.max(0, i.alert_count - 1), 0);
  const rates = active.map((i) => i.ewma_rate).filter((r) => r > 0);
  const meanRate = rates.length ? rates.reduce((a, b) => a + b, 0) / rates.length : 0;

  return { until: new Date(next), absorbed, meanRate, tracking: rates.length };
}
