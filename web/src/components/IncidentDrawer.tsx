"use client";

import { useEffect } from "react";
import { clockTime, relativeTime } from "@/lib/format";
import {
  LIFECYCLE,
  ROUTE_BADGE,
  SEVERITY,
  burstMeaning,
  routeLabel,
  routingReason,
} from "@/lib/theme";
import type { Incident, IncidentEdge } from "@/lib/types";

function Block({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <section className="border-b border-edge px-5 py-4">
      <h3 className="text-[11px] font-medium uppercase tracking-wide text-text-3">{title}</h3>
      <div className="mt-2.5">{children}</div>
    </section>
  );
}

function Related({
  incidents,
  onSelect,
  empty,
}: {
  incidents: Incident[];
  onSelect: (incident: Incident) => void;
  empty: string;
}) {
  if (incidents.length === 0) return <p className="text-[12px] text-text-2">{empty}</p>;
  return (
    <ul className="space-y-1">
      {incidents.map((incident) => (
        <li key={incident.incident_id}>
          <button
            type="button"
            onClick={() => onSelect(incident)}
            className="flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors hover:bg-panel-2"
          >
            <span
              className={`size-1.5 shrink-0 rounded-full ${SEVERITY[incident.severity].dot}`}
              aria-hidden
            />
            <span className="min-w-0 flex-1 truncate text-[13px] text-text">{incident.title}</span>
            <span className="font-mono text-[12px] tabular-nums text-text-2">
              {incident.alert_count}
            </span>
          </button>
        </li>
      ))}
    </ul>
  );
}

export function IncidentDrawer({
  incident,
  incidents,
  edges,
  onSelect,
  onClose,
}: {
  incident: Incident;
  incidents: Incident[];
  edges: IncidentEdge[];
  onSelect: (incident: Incident) => void;
  onClose: () => void;
}) {
  const severity = SEVERITY[incident.severity];
  const lifecycle = LIFECYCLE[incident.status];
  const route = incident.route_decision;

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [onClose]);

  const byId = new Map(incidents.map((i) => [i.incident_id, i]));
  const causes = edges
    .filter((e) => e.dst_incident_id === incident.incident_id)
    .map((e) => byId.get(e.src_incident_id))
    .filter((i): i is Incident => Boolean(i));
  const effects = edges
    .filter((e) => e.src_incident_id === incident.incident_id)
    .map((e) => byId.get(e.dst_incident_id))
    .filter((i): i is Incident => Boolean(i));

  const duration = (() => {
    if (!incident.first_alert_at || !incident.last_alert_at) return null;
    const ms =
      new Date(incident.last_alert_at).getTime() - new Date(incident.first_alert_at).getTime();
    const mins = Math.max(1, Math.round(ms / 60000));
    return mins < 60 ? `${mins} min` : `${Math.round((mins / 60) * 10) / 10} h`;
  })();

  return (
    <aside className="flex w-full shrink-0 flex-col overflow-y-auto border-l border-edge bg-panel lg:w-[400px]">
      <header className="flex items-start justify-between gap-3 border-b border-edge px-5 py-4">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-1.5">
            <span
              className={`inline-flex rounded px-2 py-0.5 text-[12px] font-medium ${severity.badge}`}
            >
              {severity.label}
            </span>
            <span
              className={`inline-flex items-center gap-1.5 rounded px-2 py-0.5 text-[12px] font-medium ${lifecycle.badge}`}
            >
              <span className={`size-1.5 rounded-full ${lifecycle.dot}`} aria-hidden />
              {lifecycle.label}
            </span>
          </div>
          <h2 className="mt-2 text-[15px] font-semibold text-text">{incident.title}</h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close incident details"
          className="shrink-0 rounded-md px-2 py-1 text-[12px] text-text-2 transition-colors hover:bg-panel-2 hover:text-text"
        >
          Close
        </button>
      </header>

      {incident.summary && (
        <Block title="Summary">
          <p className="text-[13px] text-text">{incident.summary}</p>
        </Block>
      )}

      <Block title="Alert consolidation">
        <p className="font-mono text-[30px] font-semibold leading-none tabular-nums text-text">
          {incident.alert_count.toLocaleString()}
        </p>
        <p className="mt-1.5 text-[12px] text-text-2">
          near identical alerts collapsed into this one incident
        </p>
        <dl className="mt-3 flex flex-wrap gap-x-8 gap-y-2 text-[12px]">
          <div>
            <dt className="text-text-2">EWMA rate</dt>
            <dd className="font-mono tabular-nums text-text">
              {incident.ewma_rate.toFixed(2)}/min
              <span className="ml-1.5 font-sans text-text-2">
                {burstMeaning(incident.ewma_rate)}
              </span>
            </dd>
          </div>
          {duration && (
            <div>
              <dt className="text-text-2">Active for</dt>
              <dd className="font-mono tabular-nums text-text">{duration}</dd>
            </div>
          )}
        </dl>
      </Block>

      <Block title="Root cause">
        {incident.root_cause_hint && (
          <p className="mb-3 text-[13px] text-text">{incident.root_cause_hint}</p>
        )}
        <p className="mb-1 text-[12px] text-text-2">Upstream</p>
        <Related
          incidents={causes}
          onSelect={onSelect}
          empty="Nothing upstream. This is the origin incident."
        />
        <p className="mb-1 mt-3 text-[12px] text-text-2">Downstream</p>
        <Related incidents={effects} onSelect={onSelect} empty="No downstream incidents." />
      </Block>

      <Block title="Delivery">
        {route ? (
          <span
            className={`inline-flex rounded px-2 py-0.5 text-[12px] font-medium ${
              ROUTE_BADGE[route] ?? "bg-[#F1F5F9] text-[#475569]"
            }`}
          >
            {routeLabel(route)}
          </span>
        ) : (
          <span className="text-[12px] text-text-2">Not delivered</span>
        )}
        <p className="mt-2 text-[13px] text-text-2">
          {routingReason(incident.severity, incident.route_decision)}
        </p>
      </Block>

      <Block title="Timeline">
        <dl className="space-y-2 text-[13px]">
          <div className="flex justify-between gap-4">
            <dt className="text-text-2">First alert</dt>
            <dd className="font-mono tabular-nums text-text">
              {incident.first_alert_at ? clockTime(incident.first_alert_at) : "unknown"}
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-text-2">Last alert</dt>
            <dd className="font-mono tabular-nums text-text">
              {incident.last_alert_at ? relativeTime(incident.last_alert_at) : "unknown"}
            </dd>
          </div>
          <div className="flex justify-between gap-4">
            <dt className="text-text-2">State</dt>
            <dd className="text-right text-text">{lifecycle.note}</dd>
          </div>
        </dl>
      </Block>

      <div className="px-5 py-4">
        <p className="text-[11px] font-medium uppercase tracking-wide text-text-3">Incident ID</p>
        <p className="mt-1 break-all font-mono text-[12px] text-text-2">{incident.incident_id}</p>
      </div>
    </aside>
  );
}
