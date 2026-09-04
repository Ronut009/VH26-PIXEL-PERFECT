"use client";

import { relativeTime } from "@/lib/format";
import {
  LIFECYCLE,
  LIFECYCLE_ORDER,
  ROUTE_BADGE,
  SEVERITY,
  byUrgency,
  rootCauseOf,
  routeLabel,
} from "@/lib/theme";
import type { Incident, Lifecycle } from "@/lib/types";

function Badge({ className, children }: { className: string; children: React.ReactNode }) {
  return (
    <span
      className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[12px] font-medium ${className}`}
    >
      {children}
    </span>
  );
}

export function IncidentsTable({
  incidents,
  loading,
  onSelect,
  selectedId,
  filter,
  onFilter,
  query,
  onQuery,
}: {
  incidents: Incident[];
  loading: boolean;
  onSelect: (incident: Incident) => void;
  selectedId: string | null;
  filter: Lifecycle | "ALL";
  onFilter: (next: Lifecycle | "ALL") => void;
  query: string;
  onQuery: (next: string) => void;
}) {
  const counts = LIFECYCLE_ORDER.reduce<Record<string, number>>((acc, state) => {
    acc[state] = incidents.filter((i) => i.status === state).length;
    return acc;
  }, {});

  const rows = incidents
    .filter((i) => filter === "ALL" || i.status === filter)
    .filter((i) => (query ? i.title.toLowerCase().includes(query.toLowerCase()) : true))
    .sort(byUrgency);

  const tabs: { key: Lifecycle | "ALL"; label: string; count: number }[] = [
    { key: "ALL", label: "All", count: incidents.length },
    ...LIFECYCLE_ORDER.map((state) => ({
      key: state,
      label: LIFECYCLE[state].label,
      count: counts[state] ?? 0,
    })),
  ];

  return (
    <section className="rounded-lg border border-edge bg-panel">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-edge px-5 py-3">
        <div className="flex items-center gap-1">
          {tabs.map((tab) => {
            const active = filter === tab.key;
            return (
              <button
                key={tab.key}
                type="button"
                onClick={() => onFilter(tab.key)}
                className={`rounded-md px-3 py-1.5 text-[13px] transition-colors ${
                  active
                    ? "bg-brand-soft font-medium text-brand"
                    : "text-text-2 hover:bg-panel-2 hover:text-text"
                }`}
              >
                {tab.label}
                <span className={`ml-1.5 font-mono tabular-nums ${active ? "text-brand" : "text-text-3"}`}>
                  {tab.count}
                </span>
              </button>
            );
          })}
        </div>

        <div className="flex items-center gap-2">
          <label className="relative">
            <span className="sr-only">Search incidents</span>
            <input
              value={query}
              onChange={(event) => onQuery(event.target.value)}
              placeholder="Search incidents"
              className="w-52 rounded-md border border-edge bg-panel py-1.5 pl-8 pr-3 text-[13px] text-text placeholder:text-text-3 focus:border-brand focus:outline-none"
            />
            <svg
              className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-text-3"
              width="14"
              height="14"
              viewBox="0 0 16 16"
              fill="none"
              stroke="currentColor"
              strokeWidth="1.5"
            >
              <circle cx="7" cy="7" r="4.5" />
              <path d="m10.5 10.5 3 3" strokeLinecap="round" />
            </svg>
          </label>
        </div>
      </div>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[860px] border-collapse text-left">
          <thead>
            <tr className="border-b border-edge text-[11px] uppercase tracking-wide text-text-3">
              <th className="px-5 py-2.5 font-medium">Incident</th>
              <th className="px-3 py-2.5 font-medium">Severity</th>
              <th className="px-3 py-2.5 font-medium">Status</th>
              <th className="px-3 py-2.5 font-medium">Delivery</th>
              <th className="px-3 py-2.5 font-medium">Root cause</th>
              <th className="px-3 py-2.5 text-right font-medium">Alerts</th>
              <th className="px-5 py-2.5 text-right font-medium">Last update</th>
            </tr>
          </thead>
          <tbody>
            {loading
              ? Array.from({ length: 5 }).map((_, index) => (
                  <tr key={index} className="border-b border-edge/70">
                    <td colSpan={7} className="px-5 py-3.5">
                      <div className="h-3 w-1/3 rounded bg-panel-2" />
                    </td>
                  </tr>
                ))
              : rows.map((incident) => {
                  const severity = SEVERITY[incident.severity];
                  const lifecycle = LIFECYCLE[incident.status];
                  const route = incident.route_decision;
                  const selected = incident.incident_id === selectedId;

                  return (
                    <tr
                      key={incident.incident_id}
                      onClick={() => onSelect(incident)}
                      aria-selected={selected}
                      tabIndex={0}
                      onKeyDown={(event) => {
                        if (event.key === "Enter" || event.key === " ") {
                          event.preventDefault();
                          onSelect(incident);
                        }
                      }}
                      className={`cursor-pointer border-b border-edge/70 transition-colors ${
                        selected ? "bg-brand-soft" : "hover:bg-panel-2"
                      }`}
                    >
                      <td className="px-5 py-3.5">
                        <div className="flex items-start gap-2.5">
                          <span
                            className={`mt-1.5 size-1.5 shrink-0 rounded-full ${severity.dot}`}
                            aria-hidden
                          />
                          <div className="min-w-0">
                            <p className="truncate text-[13px] font-medium text-text">
                              {incident.title}
                            </p>
                            {incident.root_cause_hint && (
                              <p className="mt-0.5 truncate text-[12px] text-text-2">
                                {incident.root_cause_hint}
                              </p>
                            )}
                          </div>
                        </div>
                      </td>
                      <td className="px-3 py-3.5">
                        <Badge className={severity.badge}>{severity.label}</Badge>
                      </td>
                      <td className="px-3 py-3.5">
                        <Badge className={lifecycle.badge}>
                          <span className={`size-1.5 rounded-full ${lifecycle.dot}`} aria-hidden />
                          {lifecycle.label}
                        </Badge>
                      </td>
                      <td className="px-3 py-3.5">
                        {route ? (
                          <Badge className={ROUTE_BADGE[route] ?? "bg-[#F1F5F9] text-[#475569]"}>
                            {routeLabel(route)}
                          </Badge>
                        ) : (
                          <span className="text-[12px] text-text-3">Not sent</span>
                        )}
                      </td>
                      <td className="px-3 py-3.5 text-[13px] text-text-2">
                        {rootCauseOf(incident.title)}
                      </td>
                      <td className="px-3 py-3.5 text-right font-mono text-[13px] tabular-nums text-text">
                        {incident.alert_count.toLocaleString()}
                      </td>
                      <td className="px-5 py-3.5 text-right font-mono text-[12px] tabular-nums text-text-2">
                        {incident.last_alert_at ? relativeTime(incident.last_alert_at) : ""}
                      </td>
                    </tr>
                  );
                })}
          </tbody>
        </table>

        {!loading && rows.length === 0 && (
          <div className="px-5 py-14 text-center">
            <p className="text-[13px] font-medium text-text">No incidents match this view</p>
            <p className="mt-1 text-[12px] text-text-2">
              Alerts posted to /v1/ingest appear here once deduplicated.
            </p>
          </div>
        )}
      </div>
    </section>
  );
}
