"use client";

import { useMemo, useState } from "react";
import { useSearchParams } from "next/navigation";
import { MobileNav, Sidebar, type ViewKey } from "@/components/Sidebar";
import { TopHeader } from "@/components/TopHeader";
import {
  AlertVolumeChart,
  Kpi,
  Panel,
  QuietWindow,
  RecentActivity,
} from "@/components/OverviewPanels";
import { IncidentsTable, type SortMode } from "@/components/IncidentsTable";
import { AuditLedger, DeliveriesTable } from "@/components/OperationalTables";
import { CorrelationGraph } from "@/components/CorrelationGraph";
import { CodeInvestigation } from "@/components/CodeInvestigation";
import { IncidentDrawer } from "@/components/IncidentDrawer";
import { usePulseGraphStream } from "@/hooks/usePulseGraphStream";
import { useBackendHealth } from "@/hooks/useBackendHealth";
import { investigationAvailability, useGithubReadiness } from "@/hooks/useGithubReadiness";
import { totals } from "@/lib/metrics";
import { serviceOf } from "@/lib/theme";
import { DEMO_INCIDENTS, DEMO_EDGES } from "@/lib/demoData";
import type { DashboardUser, Incident, Lifecycle, Severity } from "@/lib/types";

const TITLES: Record<ViewKey, string> = {
  overview: "Overview",
  incidents: "Incidents",
  correlations: "Correlations",
  deliveries: "Deliveries",
  audit: "Audit Ledger",
  github: "Code Investigation",
  settings: "Settings",
};

export function DashboardApp({ user }: { user: DashboardUser }) {
  const { incidents, edges, state, lastUpdated, loading } = usePulseGraphStream();
  const backend = useBackendHealth();
  const { readiness, reload: recheckGithub } = useGithubReadiness();

  const [view, setView] = useState<ViewKey>("overview");
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [investigationId, setInvestigationId] = useState<string | null>(null);
  const [filter, setFilter] = useState<Lifecycle | "ALL">("ALL");
  const [query, setQuery] = useState("");
  const [severities, setSeverities] = useState<readonly Severity[]>([]);
  const [sort, setSort] = useState<SortMode>("urgency");

  // Sample data is opt-in via ?demo=1 and never substitutes automatically.
  // It used to appear whenever the backend was unreachable, which made an
  // empty database look like a console still holding 347 alerts, and made
  // "demo.py --reset" look broken when it had in fact worked correctly.
  const isSample = useSearchParams().get("demo") === "1";
  const shown = isSample ? DEMO_INCIDENTS : incidents;
  const shownEdges = isSample ? DEMO_EDGES : edges;
  const connected = state === "live" || state === "polling";

  const selected = useMemo(
    () => shown.find((i) => i.incident_id === selectedId) ?? null,
    [shown, selectedId],
  );
  const investigationTarget = useMemo(
    () => shown.find((i) => i.incident_id === investigationId) ?? null,
    [shown, investigationId],
  );

  const select = (incident: Incident) =>
    setSelectedId((cur) => (cur === incident.incident_id ? null : incident.incident_id));

  /**
   * Hand an incident to the Code Investigation view. The drawer closes because
   * below `lg` it covers the page entirely — leaving it open would hide the
   * view the operator just asked for.
   */
  const investigate = (incident: Incident) => {
    setInvestigationId(incident.incident_id);
    setView("github");
    setSelectedId(null);
  };

  const t = totals(shown);
  const active = shown.filter((i) => i.status !== "RESOLVED").length;
  const critical = shown.filter((i) => i.severity === "critical").length;
  const consolidated = t.alertsIn - t.surfaced;

  // Reported, not inferred: API and Database come from GET /v1/health, and the
  // outbox worker has no health endpoint, so it says so rather than guessing.
  const health: Record<string, boolean | null> = {
    api: backend.state === "unknown" ? null : backend.apiReachable,
    sse: state === "connecting" ? null : state === "live",
    db: backend.state === "unknown" ? null : backend.databaseHealthy,
    outbox: null,
  };

  const githubSummary =
    readiness.kind === "ready"
      ? `${readiness.repositories.length} repositories readable`
      : readiness.kind === "loading"
        ? "checking"
        : readiness.kind;

  return (
    <div className="flex h-dvh bg-app">
      <Sidebar view={view} onView={setView} connected={connected} health={health} />

      <div className="flex min-h-0 min-w-0 flex-1 flex-col">
        <TopHeader title={TITLES[view]} connected={connected} lastEvent={lastUpdated} user={user} />
        <MobileNav view={view} onView={setView} />

        <main className="flex min-h-0 flex-1">
          <div className="min-h-0 flex-1 overflow-y-auto">
            <div className="mx-auto max-w-[1400px] px-4 py-5 sm:px-6 sm:py-6">
              {isSample && (
                <p className="mb-5 rounded-md border border-[#FDE68A] bg-[#FFFBEB] px-4 py-2.5 text-[12px] text-[#B45309]">
                  Sample data (<span className="font-mono">?demo=1</span>). These figures are a
                  fixture, not your database. Drop the query parameter to see real pipeline state.
                </p>
              )}

              {backend.state === "degraded" && backend.message && (
                <p className="mb-5 rounded-md border border-[#FDE68A] bg-[#FFFBEB] px-4 py-2.5 text-[12px] text-[#B45309]">
                  {backend.message}
                  {backend.action ? ` ${backend.action}` : ""}
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

              {view === "incidents" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">
                      {TITLES[view]}
                    </h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      Every alert the pipeline received, deduplicated and correlated into the
                      incidents below. Select a row to open root cause analysis.
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
                    severities={severities}
                    onSeverities={setSeverities}
                    sort={sort}
                    onSort={setSort}
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

              {view === "github" && (
                <CodeInvestigation
                  readiness={readiness}
                  onRetry={recheckGithub}
                  target={investigationTarget}
                  incidents={shown}
                />
              )}

              {view === "settings" && (
                <div className="space-y-5">
                  <div>
                    <h1 className="text-[20px] font-semibold tracking-tight text-text">Settings</h1>
                    <p className="mt-1 text-[13px] text-text-2">
                      Connection and data source for this dashboard session.
                    </p>
                  </div>
                  <Panel
                    title="Connection"
                    hint="The browser talks only to this app; the Next.js server talks to the backend"
                  >
                    <dl className="divide-y divide-edge text-[13px]">
                      {[
                        ["Stream endpoint", "GET /api/stream → /v1/stream"],
                        ["Fallback endpoint", "GET /api/incidents/recent → /v1/incidents/recent"],
                        ["Health endpoint", "GET /api/health → /v1/health"],
                        ["Stream state", state],
                        ["Backend health", backend.state],
                        ["GitHub investigation", githubSummary],
                        ["Data source", isSample ? "sample" : "backend"],
                      ].map(([label, value]) => (
                        <div key={label} className="flex justify-between gap-4 py-2.5">
                          <dt className="text-text-2">{label}</dt>
                          <dd className="text-right font-mono text-text">{value}</dd>
                        </div>
                      ))}
                    </dl>
                    {backend.message && (
                      <p className="mt-3 text-[12px] text-[#B45309]">
                        {backend.message}
                        {backend.action ? ` ${backend.action}` : ""}
                      </p>
                    )}
                  </Panel>

                  <Panel title="Credentials" hint="Where each value is read, and by what">
                    <dl className="divide-y divide-edge text-[13px]">
                      <div className="flex justify-between gap-4 pb-2.5">
                        <dt className="text-text-2">PULSEGRAPH_API_BASE</dt>
                        <dd className="text-right text-text">
                          Server only. Read by the route handlers.
                        </dd>
                      </div>
                      <div className="flex justify-between gap-4 pt-2.5">
                        <dt className="text-text-2">GITHUB_ADMIN_TOKEN</dt>
                        <dd className="text-right text-text">
                          Server only. Attached after the request leaves the browser.
                        </dd>
                      </div>
                    </dl>
                    <p className="mt-3 text-[12px] text-text-2">
                      Neither value carries a <span className="font-mono">NEXT_PUBLIC_</span>{" "}
                      prefix, so neither is present in the client bundle.
                    </p>
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
              onInvestigate={investigate}
              investigation={investigationAvailability(
                readiness,
                serviceOf(selected.title),
                isSample,
              )}
            />
          )}
        </main>
      </div>
    </div>
  );
}
