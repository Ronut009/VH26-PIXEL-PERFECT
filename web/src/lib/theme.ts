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
    badge: "bg-[#3A1915] text-[#FF9D93]",
    dot: "bg-[#FF6B5E]",
    rank: 0,
    meaning: "Bypasses aggregation and pages on-call immediately",
  },
  high: {
    label: "High",
    badge: "bg-[#38270E] text-[#FFC66E]",
    dot: "bg-[#FFB54A]",
    rank: 1,
    meaning: "Needs an engineer today",
  },
  medium: {
    label: "Medium",
    badge: "bg-[#113447] text-[#7BE5FF]",
    dot: "bg-[#5DE4FF]",
    rank: 2,
    meaning: "Worth reviewing, not urgent",
  },
  low: {
    label: "Low",
    badge: "bg-[#252B22] text-[#B7C0AD]",
    dot: "bg-[#7F8A77]",
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
    badge: "bg-[#3A1915] text-[#FF9D93]",
    dot: "bg-[#FF6B5E]",
    note: "Still firing, not yet acknowledged",
  },
  ACKNOWLEDGED: {
    label: "Acknowledged",
    badge: "bg-[#283414] text-[#E3FF7A]",
    dot: "bg-[#C8FF3D]",
    note: "An engineer has taken ownership",
  },
  QUIESCENT: {
    label: "Quiet",
    badge: "bg-[#252B22] text-[#B7C0AD]",
    dot: "bg-[#7F8A77]",
    note: "Inside the adaptive quiet window",
  },
  RESOLVED: {
    label: "Resolved",
    badge: "bg-[#183417] text-[#A6EA91]",
    dot: "bg-[#88DF70]",
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
  pagerduty: "bg-[#3A1915] text-[#FF9D93]",
  slack: "bg-[#113447] text-[#7BE5FF]",
  email: "bg-[#252B22] text-[#B7C0AD]",
  suppressed: "bg-[#183417] text-[#A6EA91]",
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

/** Current backend hint: `<incident title> (confidence 94%)`. */
const CONFIDENCE_HINT = /\(confidence \d+%\)$/;

/** Pre-confidence backend hint, kept for rows written before the change. */
const LEGACY_HINT =
  /^root_cause=([0-9a-fA-F-]{36});\s*outbound_decayed_joint_weight=([\d.]+)$/;

/**
 * Turn the ranker's hint into something an engineer can read.
 *
 * The backend used to emit `root_cause=<uuid>; outbound_decayed_joint_weight=
 * <score>` and leave the resolving to us, which is why the legacy branch below
 * looks the incident up by id. It now does that work itself and emits
 * `<incident title> (confidence 94%)`, for two reasons: a bare UUID and a raw
 * weight tell a responder nothing at 3am, and the same string is rendered
 * verbatim on the Slack card, where there is no incident list to resolve
 * against.
 *
 * The confidence matters as much as the name. The ranker now declines to
 * answer when the evidence is thin — a hint that is present is one it was
 * willing to stand behind, and the percentage says how far.
 *
 * Both shapes are handled, and anything unrecognised is still passed through
 * untouched rather than dropped, so a future format keeps showing up.
 */
export function describeRootCause(
  hint: string | null,
  incidents: { incident_id: string; title: string }[],
): string | null {
  if (!hint) return null;
  const trimmed = hint.trim();
  if (!trimmed) return null;

  if (CONFIDENCE_HINT.test(trimmed)) {
    return `Likely root cause: ${trimmed}`;
  }

  const match = LEGACY_HINT.exec(trimmed);
  if (!match) return trimmed;

  const [, incidentId, weight] = match;
  const cause = incidents.find((incident) => incident.incident_id === incidentId);
  const score = Number(weight);
  const strength = Number.isFinite(score) ? ` (joint weight ${score.toFixed(1)})` : "";
  return cause
    ? `Likely root cause: ${cause.title}${strength}`
    : `Likely root cause: an incident no longer in view${strength}`;
}
