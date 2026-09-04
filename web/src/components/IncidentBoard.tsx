import { IncidentCard } from "./IncidentCard";
import { STATUS_LABEL, STATUS_ORDER } from "@/lib/theme";
import type { Incident } from "@/lib/types";

function severityRank(incident: Incident): number {
  return { critical: 0, high: 1, medium: 2, low: 3 }[incident.severity];
}

export function IncidentBoard({
  incidents,
  onSelect,
}: {
  incidents: Incident[];
  onSelect: (incident: Incident) => void;
}) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-4 gap-3 flex-1 min-h-0">
      {STATUS_ORDER.map((status) => {
        const column = incidents
          .filter((i) => i.status === status)
          .sort((a, b) => severityRank(a) - severityRank(b) || b.updated_at.localeCompare(a.updated_at));

        return (
          <div key={status} className="flex flex-col min-h-0 border border-(--hairline)">
            <div className="flex items-center justify-between px-3 py-2 border-b border-(--hairline) bg-(--bg-inset)">
              <span className="text-[11px] font-(family-name:--font-data) uppercase tracking-wider text-(--text-dim)">
                {STATUS_LABEL[status]}
              </span>
              <span className="text-[11px] font-(family-name:--font-data) tabular-nums text-(--text-faint)">
                {column.length}
              </span>
            </div>
            <div className="flex flex-col gap-2 p-2 overflow-y-auto">
              {column.length === 0 ? (
                <p className="text-xs text-(--text-faint) px-1 py-2">none</p>
              ) : (
                column.map((incident) => (
                  <IncidentCard key={incident.incident_id} incident={incident} onSelect={onSelect} />
                ))
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
