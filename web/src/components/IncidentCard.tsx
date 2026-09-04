import { SeverityDot } from "./SeverityDot";
import { relativeTime } from "@/lib/format";
import { ROUTE_LABEL, routeColor } from "@/lib/theme";
import type { Incident } from "@/lib/types";

export function IncidentCard({
  incident,
  onSelect,
}: {
  incident: Incident;
  onSelect: (incident: Incident) => void;
}) {
  const route = incident.route_decision;

  return (
    <button
      type="button"
      onClick={() => onSelect(incident)}
      className="w-full text-left border border-(--hairline) bg-(--bg-raised) p-3 hover:bg-(--bg-inset) transition-colors duration-100 flex flex-col gap-2"
    >
      <div className="flex items-start gap-2">
        <SeverityDot severity={incident.severity} />
        <span className="text-sm leading-snug flex-1 min-w-0 truncate">{incident.title}</span>
        <span className="font-(family-name:--font-data) text-xs text-(--text-dim) tabular-nums shrink-0">
          ×{incident.alert_count}
        </span>
      </div>

      {incident.root_cause_hint && (
        <p className="text-xs text-(--text-dim) truncate pl-4">{incident.root_cause_hint}</p>
      )}

      <div className="flex items-center justify-between pl-4">
        <span className="text-[11px] text-(--text-faint)">{relativeTime(incident.last_alert_at)}</span>
        {route && (
          <span
            className="text-[11px] font-(family-name:--font-data) uppercase tracking-wide"
            style={{ color: routeColor(route) }}
          >
            {ROUTE_LABEL[route] ?? route}
          </span>
        )}
      </div>
    </button>
  );
}
