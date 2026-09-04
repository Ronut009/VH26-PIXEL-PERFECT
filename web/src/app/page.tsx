"use client";

import { useMemo, useState } from "react";
import { Sidebar, type ViewKey } from "@/components/Sidebar";
import { TopHeader } from "@/components/TopHeader";
import {
  AlertVolumeChart,
  Kpi,
  Panel,
  QuietWindow,
  RecentActivity,
} from "@/components/OverviewPanels";
import { IncidentsTable } from "@/components/IncidentsTable";
import { AuditLedger, DeliveriesTable } from "@/components/OperationalTables";
import { CorrelationGraph } from "@/components/CorrelationGraph";
import { IncidentDrawer } from "@/components/IncidentDrawer";
import { usePulseGraphStream } from "@/hooks/usePulseGraphStream";
import { totals } from "@/lib/metrics";
import { DEMO_INCIDENTS, DEMO_EDGES } from "@/lib/demoData";
import type { Incident, Lifecycle } from "@/lib/types";

const TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  incidents: "Incidents",
  alerts: "Alerts",
  correlations: "Correlations",
  deliveries: "Deliveries",
  audit: "Audit Ledger",
  settings: "Settings",
};

export default function Home() {
  const { incidents, edges, state, lastUpdated, loading } = usePulseGraphStream();
  const [view, setView] = useState<ViewKey>("overview");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [filter, setFilter] = useState<Lifecycle | "ALL">("ALL");
  const [query, setQuery] = useState("");

  // Sample data appears only when the backend is unreachable, and is labelled.
  const isSample = state === "offline" && incidents.length === 0;
  const shown = isSample ? DEMO_INCIDENTS : incidents;
  const shownEdges = isSample ? DEMO_EDGES : edges;
  const connected = state === "live" || state === "polling";

  const selected = useMemo(
    () => shown.find((i) => i.incident_id === selectedId) ?? null,
    [shown, selectedId],
  );

  const select = (incident: Incident) =>
    setSelectedId((cur) => (cur === incident.incident_id ? null : incident.incident_id));

  const t = totals(shown);
  const active = shown.filter((i) => i.status !== "RESOLVED").length;
  const critical = shown.filter((i) => i.severity === "critical").length;
  const consolidated = t.alertsIn - t.surfaced;

  const health = {
    api: connected,
    sse: state === "live",
    outbox: connected,
    db: connected,
  };

  return (
    <div className="flex h-dvh bg-app">
      <Sidebar view={view} onView={setView} connected={connected} health={health} />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopHeader title={TITLES[view]} connected={connected} lastEvent={lastUpdated} />

        <main className="flex min-h-0 flex-1">
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-[1400px] px-6 py-6">
              {isSample && (
                <p className="mb-5 rounded-md border border-edge bg-[#FFFBEB] px-4 py-2.5 text-[12px] text-[#B45309]">
                  Backend unreachable. Showing sample incidents so the console stays inspectable.
                  Every figure below is derived from these records.
                </p>
              )}

              {view === "overview" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">Overview</h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      PulseGraph is watching {shown.length} correlated{" "}
                      {shown.length === 1 ? "incident" : "incidents"} across your services and
                      holding back everything that is a duplicate.
                    </p>
                  </div>

                  <div className="grid grid-cols-1 gap-4 sm:grid-cols-2 xl:grid-cols-4">
                    <Kpi
                      label="Active incidents"
                      value={active.toLocaleString()}
                      sub={`${shown.length - active} resolved`}
                    />
                    <Kpi
                      label="Alerts ingested"
                      value={t.alertsIn.toLocaleString()}
                      sub="raw webhooks received"
                    />
                    <Kpi
                      label="Alerts consolidated"
                      value={`${t.reduction.toFixed(1)}%`}
                      tone="text-[#15803D]"
                      emphasis
                      sub={
                        <span>
                          <span className="font-mono tabular-nums text-text">
                            {consolidated.toLocaleString()}
                          </span>{" "}
                          duplicates suppressed
                        </span>
                      }
                    />
                    <Kpi
                      label="Critical incidents"
                      value={critical.toLocaleString()}
                      tone={critical > 0 ? "text-[#B91C1C]" : "text-text"}
                      sub={`${t.paged} paged through PagerDuty`}
                    />
                  </div>

                  <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
                    <Panel
                      title="Alert volume"
                      hint="Ingested alerts against the incidents they were consolidated into"
                    >
                      <AlertVolumeChart incidents={shown} />
                    </Panel>

                    <Panel
                      title="Adaptive quiet window"
                      hint="Signal-driven deadline set by the engine"
                    >
                      <QuietWindow incidents={shown} />
                    </Panel>
                  </div>

                  <div className="grid grid-cols-1 gap-4 xl:grid-cols-[minmax(0,1fr)_340px]">
                    <Panel
                      title="Co-occurrence graph"
                      hint="Incidents that keep firing together, arranged cause above effect"
                    >
                      <div className="h-[300px]">
                        <CorrelationGraph
                          incidents={shown}
                          edges={shownEdges}
                          onSelect={select}
                          selectedId={selectedId}
                        />
                      </div>
                    </Panel>

                    <Panel title="Recent activity" hint="Most recently updated incidents">
                      <RecentActivity incidents={shown} onSelect={select} />
                    </Panel>
                  </div>
                </div>
              )}

              {(view === "incidents" || view === "alerts") && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">
                      {TITLES[view]}
                    </h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      {view === "incidents"
                        ? "Deduplicated and correlated. Select a row to open root cause analysis."
                        : "Every consolidated signal, newest first. Alerts are grouped into the incidents below."}
                    </p>
                  </div>
                  <IncidentsTable
                    incidents={shown}
                    loading={loading && shown.length === 0}
                    onSelect={select}
                    selectedId={selectedId}
                    filter={filter}
                    onFilter={setFilter}
                    query={query}
                    onQuery={setQuery}
                  />
                </div>
              )}

              {view === "correlations" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">
                      Correlations
                    </h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      Incidents that keep firing together, arranged cause above effect. Edge weight
                      is the decayed joint weight recorded by the co-occurrence graph.
                    </p>
                  </div>
                  <Panel className="p-0">
                    <div className="h-[calc(100dvh-15rem)] min-h-[420px]">
                      <CorrelationGraph
                        incidents={shown}
                        edges={shownEdges}
                        onSelect={select}
                        selectedId={selectedId}
                      />
                    </div>
                  </Panel>
                </div>
              )}

              {view === "deliveries" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">
                      Deliveries
                    </h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      Outbound notifications produced by the transactional outbox.
                    </p>
                  </div>
                  <DeliveriesTable incidents={shown} onSelect={select} />
                </div>
              )}

              {view === "audit" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">
                      Audit Ledger
                    </h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      Hash-chained record of every event that entered the pipeline.
                    </p>
                  </div>
                  <AuditLedger incidents={shown} />
                </div>
              )}

              {view === "settings" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">Settings</h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      Connection and data source for this dashboard session.
                    </p>
                  </div>
                  <Panel title="Connection">
                    <dl className="divide-y divide-edge text-[13px]">
                      {[
                        ["Stream endpoint", "GET /v1/stream"],
                        ["Fallback endpoint", "GET /v1/incidents/recent"],
                        ["Connection state", state],
                        ["Data source", isSample ? "sample" : "backend"],
                      ].map(([label, value]) => (
                        <div key={label} className="flex justify-between gap-4 py-2.5">
                          <dt className="text-text-2">{label}</dt>
                          <dd className="font-mono text-text">{value}</dd>
                        </div>
                      ))}
                    </dl>
                  </Panel>
                </div>
              )}
            </div>
          </div>

          {selected && (
            <IncidentDrawer
              incident={selected}
              incidents={shown}
              edges={shownEdges}
              onSelect={select}
              onClose={() => setSelectedId(null)}
            />
          )}
        </main>
      </div>
    </div>
  );
}
