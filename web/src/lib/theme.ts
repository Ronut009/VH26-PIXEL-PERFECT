import type { Severity, RouteDecision, IncidentStatus } from "./types";

// Single source of truth for severity/status color + copy, so every
// component renders them the same way instead of re-deciding per file.
export const SEVERITY_COLOR: Record<Severity, string> = {
  critical: "var(--sev-critical)",
  high: "var(--sev-high)",
  medium: "var(--sev-medium)",
  low: "var(--sev-low)",
};

export const SEVERITY_ORDER: Severity[] = ["critical", "high", "medium", "low"];

export const STATUS_ORDER: IncidentStatus[] = ["new", "active", "quiet", "resolved"];

export const STATUS_LABEL: Record<IncidentStatus, string> = {
  new: "New",
  active: "Active",
  quiet: "Quiet",
  resolved: "Resolved",
};

export const ROUTE_LABEL: Record<string, string> = {
  slack: "Slack",
  pagerduty: "PagerDuty",
  email: "Email",
  suppressed: "Suppressed",
};

export function routeColor(route: RouteDecision): string {
  if (route === "suppressed") return "var(--route-suppressed)";
  if (route === "pagerduty") return "var(--sev-critical)";
  return "var(--text-dim)";
}
