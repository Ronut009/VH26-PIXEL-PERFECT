import type { Severity, Lifecycle, RouteDecision } from "./types";

/**
 * Semantic badge treatments. The brief's palette hexes are kept for fills,
 * dots and chart strokes; badge TEXT uses a darker step of the same hue so it
 * clears WCAG AA on its tint (measured: crit 5.91:1, warn 4.84:1, ok 4.79:1,
 * brand 6.08:1, neutral 6.92:1).
 */
export const SEVERITY: Record<
  Severity,
  { label: string; badge: string; dot: string; rank: number; meaning: string }
> = {
  critical: {
    label: "Critical",
    badge: "bg-[#FEF2F2] text-[#B91C1C]",
    dot: "bg-[#EF4444]",
    rank: 0,
    meaning: "Bypasses aggregation and pages on-call immediately",
  },
  high: {
    label: "High",
    badge: "bg-[#FFFBEB] text-[#B45309]",
    dot: "bg-[#F59E0B]",
    rank: 1,
    meaning: "Needs an engineer today",
  },
  medium: {
    label: "Medium",
    badge: "bg-[#EFF4FF] text-[#1D4ED8]",
    dot: "bg-[#2563EB]",
    rank: 2,
    meaning: "Worth reviewing, not urgent",
  },
  low: {
    label: "Low",
    badge: "bg-[#F1F5F9] text-[#475569]",
    dot: "bg-[#94A3B8]",
    rank: 3,
    meaning: "Background noise",
  },
};

export const LIFECYCLE_ORDER: Lifecycle[] = ["OPEN", "ACKNOWLEDGED", "QUIESCENT", "RESOLVED"];

export const LIFECYCLE: Record<
  Lifecycle,
  { label: string; badge: string; dot: string; note: string }
> = {
  OPEN: {
    label: "Open",
    badge: "bg-[#FEF2F2] text-[#B91C1C]",
    dot: "bg-[#EF4444]",
    note: "Still firing, not yet acknowledged",
  },
  ACKNOWLEDGED: {
    label: "Acknowledged",
    badge: "bg-[#EFF4FF] text-[#1D4ED8]",
    dot: "bg-[#2563EB]",
    note: "An engineer has taken ownership",
  },
  QUIESCENT: {
    label: "Quiet",
    badge: "bg-[#F1F5F9] text-[#475569]",
    dot: "bg-[#94A3B8]",
    note: "Inside the adaptive quiet window",
  },
  RESOLVED: {
    label: "Resolved",
    badge: "bg-[#F0FDF4] text-[#15803D]",
    dot: "bg-[#16A34A]",
    note: "Closed out, no longer firing",
  },
};

export const ROUTE_LABEL: Record<string, string> = {
  slack: "Slack",
  pagerduty: "PagerDuty",
  email: "Email",
  suppressed: "Suppressed",
};

export const ROUTE_BADGE: Record<string, string> = {
  pagerduty: "bg-[#FEF2F2] text-[#B91C1C]",
  slack: "bg-[#EFF4FF] text-[#1D4ED8]",
  email: "bg-[#F1F5F9] text-[#475569]",
  suppressed: "bg-[#F0FDF4] text-[#15803D]",
};

export function severityRank(severity: Severity): number {
  return SEVERITY[severity]?.rank ?? 9;
}

export function byUrgency(a: { severity: Severity; updated_at: string }, b: typeof a): number {
  return (
    severityRank(a.severity) - severityRank(b.severity) || b.updated_at.localeCompare(a.updated_at)
  );
}

export function routeLabel(route: RouteDecision): string | null {
  return route ? ROUTE_LABEL[route] ?? route : null;
}

/** The routing decision stated as the rule that produced it. */
export function routingReason(severity: Severity, route: RouteDecision): string {
  if (!route) return "No delivery has been attempted for this incident yet.";
  if (route === "pagerduty") {
    return "Critical severity triggers the critical bypass: it skips aggregation and pages on-call through PagerDuty immediately.";
  }
  if (route === "slack") {
    return `${SEVERITY[severity].label} severity is consolidated into a single Slack message rather than paging anyone.`;
  }
  if (route === "email") return "Low severity is delivered by email. Nobody is paged.";
  return "Held inside the adaptive quiet window because this signal was flapping. Nobody was interrupted.";
}

export function burstMeaning(rate: number): string {
  if (rate >= 3) return "Firing constantly";
  if (rate >= 1) return "Firing regularly";
  if (rate > 0) return "Firing occasionally";
  return "No longer firing";
}

/**
 * The service named by an incident title.
 *
 * The engine builds titles as `${service} — ${alertname}` with an em dash
 * (src/engine/process_event.py); demo incidents use a middot. Both separators
 * are accepted, because matching only one silently returns the whole title —
 * which is also the string the GitHub service-to-repository mapping is looked
 * up by.
 */
const TITLE_SEPARATOR = /\s+[—·]\s+/;

export function serviceOf(title: string): string {
  const [service] = title.split(TITLE_SEPARATOR);
  return (service ?? title).trim() || title;
}

/** The alert rule a title names, when it carries one. */
export function alertnameOf(title: string): string | null {
  const parts = title.split(TITLE_SEPARATOR);
  if (parts.length < 2) return null;
  return parts.slice(1).join(" — ").trim() || null;
}

/** Root cause service, taken from the incident title prefix used by the engine. */
export function rootCauseOf(title: string): string {
  return serviceOf(title);
}
