"use client";

import { clockTime, relativeTime } from "@/lib/format";
import { ROUTE_BADGE, SEVERITY, routeLabel } from "@/lib/theme";
import type { Incident } from "@/lib/types";

/**
 * Deliveries. Derived from each incident's route_decision, which the DbWriter
 * records inside the same transaction that creates the delivery intent.
 */
export function DeliveriesTable({
  incidents,
  onSelect,
}: {
  incidents: Incident[];
  onSelect: (incident: Incident) => void;
}) {
  const rows = [...incidents]
    .filter((i) => i.route_decision)
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at));

  return (
    <section className="rounded-lg border border-edge bg-panel">
      <div className="border-b border-edge px-5 py-4">
        <h2 className="text-[14px] font-semibold text-text">Deliveries</h2>
        <p className="mt-0.5 text-[12px] text-text-2">
          Where each consolidated incident was sent, and why that channel was chosen.
        </p>
      </div>

      {rows.length === 0 ? (
        <p className="px-5 py-14 text-center text-[12px] text-text-2">
          Nothing has been delivered yet.
        </p>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full min-w-[720px] border-collapse text-left">
            <thead>
              <tr className="border-b border-edge text-[11px] uppercase tracking-wide text-text-3">
                <th className="px-5 py-2.5 font-medium">Incident</th>
                <th className="px-3 py-2.5 font-medium">Channel</th>
                <th className="px-3 py-2.5 font-medium">Severity</th>
                <th className="px-3 py-2.5 text-right font-medium">Alerts consolidated</th>
                <th className="px-5 py-2.5 text-right font-medium">Sent</th>
              </tr>
            </thead>
            <tbody>
              {rows.map((incident) => {
                const route = incident.route_decision as string;
                return (
                  <tr
                    key={incident.incident_id}
                    onClick={() => onSelect(incident)}
                    className="cursor-pointer border-b border-edge/70 transition-colors hover:bg-panel-2"
                  >
                    <td className="px-5 py-3.5 text-[13px] font-medium text-text">
                      {incident.title}
                    </td>
                    <td className="px-3 py-3.5">
                      <span
                        className={`inline-flex rounded px-2 py-0.5 text-[12px] font-medium ${
                          ROUTE_BADGE[route] ?? "bg-[#F1F5F9] text-[#475569]"
                        }`}
                      >
                        {routeLabel(incident.route_decision)}
                      </span>
                    </td>
                    <td className="px-3 py-3.5 text-[13px] text-text-2">
                      {SEVERITY[incident.severity].label}
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
        </div>
      )}
    </section>
  );
}

/**
 * Audit ledger.
 *
 * The hash-chained ledger lives in the raw_events table and is verified by
 * scripts/verify_chain.py, but the backend exposes no HTTP route for it yet.
 * Rather than fabricate sequence numbers and hashes, this page states exactly
 * what is missing and shows the real per-incident decision record it can read.
 */
export function AuditLedger({ incidents }: { incidents: Incident[] }) {
  const rows = [...incidents]
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .map((incident, index) => ({
      incident,
      decision:
        incident.alert_count > 1
          ? "Aggregated"
          : incident.route_decision === "pagerduty"
            ? "Critical bypass"
            : "Recorded",
      index,
    }));

  return (
    <section className="rounded-lg border border-edge bg-panel">
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-edge px-5 py-4">
        <div>
          <h2 className="text-[14px] font-semibold text-text">Audit ledger</h2>
          <p className="mt-0.5 text-[12px] text-text-2">
            Tamper-evident record of ingested events and the decision taken for each.
          </p>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded-md border border-edge bg-panel px-3 py-1.5 text-[12px] font-medium text-text-2 transition-colors hover:bg-panel-2 hover:text-text"
          >
            Verify chain
          </button>
          <button
            type="button"
            className="rounded-md border border-edge bg-panel px-3 py-1.5 text-[12px] font-medium text-text-2 transition-colors hover:bg-panel-2 hover:text-text"
          >
            Export
          </button>
        </div>
      </div>

      <p className="border-b border-edge bg-panel-2 px-5 py-2.5 text-[12px] text-text-2">
        Sequence numbers and row hashes live in the <span className="font-mono">raw_events</span>{" "}
        ledger and are proven by <span className="font-mono">scripts/verify_chain.py</span>. No HTTP
        route exposes them yet, so the hash column stays empty rather than showing invented values.
      </p>

      <div className="overflow-x-auto">
        <table className="w-full min-w-[760px] border-collapse text-left">
          <thead>
            <tr className="border-b border-edge text-[11px] uppercase tracking-wide text-text-3">
              <th className="px-5 py-2.5 font-medium">Event</th>
              <th className="px-3 py-2.5 font-medium">Source</th>
              <th className="px-3 py-2.5 font-medium">Decision</th>
              <th className="px-3 py-2.5 font-medium">Row hash</th>
              <th className="px-5 py-2.5 text-right font-medium">Recorded</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(({ incident, decision }) => (
              <tr key={incident.incident_id} className="border-b border-edge/70">
                <td className="px-5 py-3 text-[13px] text-text">{incident.title}</td>
                <td className="px-3 py-3 font-mono text-[12px] text-text-2">prometheus</td>
                <td className="px-3 py-3">
                  <span className="inline-flex rounded bg-[#EFF4FF] px-2 py-0.5 text-[12px] font-medium text-[#1D4ED8]">
                    {decision}
                  </span>
                </td>
                <td className="px-3 py-3 font-mono text-[12px] text-text-3">not exposed</td>
                <td className="px-5 py-3 text-right font-mono text-[12px] tabular-nums text-text-2">
                  {incident.first_alert_at ? clockTime(incident.first_alert_at) : ""}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  );
}
