import { SeverityDot } from "./SeverityDot";
import { clockTime } from "@/lib/format";
import { ROUTE_LABEL, STATUS_LABEL } from "@/lib/theme";
import type { Incident } from "@/lib/types";

function Row({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1 py-3 border-b border-(--hairline)">
      <span className="text-[11px] font-(family-name:--font-data) uppercase tracking-wider text-(--text-dim)">
        {label}
      </span>
      <div className="text-sm">{children}</div>
    </div>
  );
}

export function IncidentDrawer({
  incident,
  onClose,
}: {
  incident: Incident;
  onClose: () => void;
}) {
  return (
    <aside className="w-full max-w-sm shrink-0 border-l border-(--hairline) bg-(--bg-raised) flex flex-col overflow-y-auto">
      <div className="flex items-start justify-between gap-3 p-4 border-b border-(--hairline)">
        <div className="flex items-start gap-2 min-w-0">
          <SeverityDot severity={incident.severity} />
          <h2 className="text-sm font-medium leading-snug">{incident.title}</h2>
        </div>
        <button
          type="button"
          onClick={onClose}
          className="text-(--text-dim) hover:bg-(--bg-inset) transition-colors duration-100 px-2 py-1 text-xs shrink-0"
        >
          close
        </button>
      </div>

      <div className="px-4">
        <Row label="Status">{STATUS_LABEL[incident.status]}</Row>

        {incident.summary && <Row label="Summary">{incident.summary}</Row>}

        {incident.root_cause_hint && <Row label="Root cause hint">{incident.root_cause_hint}</Row>}

        <Row label="Alert count">
          <span className="font-(family-name:--font-data) tabular-nums">{incident.alert_count}</span>
        </Row>

        <Row label="EWMA rate">
          <span className="font-(family-name:--font-data) tabular-nums">{incident.ewma_rate.toFixed(3)}</span>
        </Row>

        <Row label="Routed to">{incident.route_decision ? ROUTE_LABEL[incident.route_decision] : "not yet routed"}</Row>

        <Row label="First alert">
          <span className="font-(family-name:--font-data)">{clockTime(incident.first_alert_at)}</span>
        </Row>

        <Row label="Last alert">
          <span className="font-(family-name:--font-data)">{clockTime(incident.last_alert_at)}</span>
        </Row>

        <Row label="Incident ID">
          <span className="font-(family-name:--font-data) text-xs text-(--text-dim) break-all">
            {incident.incident_id}
          </span>
        </Row>
      </div>
    </aside>
  );
}
