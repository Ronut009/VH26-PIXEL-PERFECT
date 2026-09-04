"use client";

import { useMemo, useState } from "react";
import { IncidentBoard } from "@/components/IncidentBoard";
import { IncidentDrawer } from "@/components/IncidentDrawer";
import { VolumePanel } from "@/components/VolumePanel";
import { RootCauseGraph } from "@/components/RootCauseGraph";
import { ConnectionStatus } from "@/components/ConnectionStatus";
import { useIncidents } from "@/hooks/useIncidents";
import { useIncidentEdges } from "@/hooks/useIncidentEdges";
import { DEMO_INCIDENTS, DEMO_EDGES } from "@/lib/demoData";
import type { Incident } from "@/lib/types";

export default function Home() {
  const { incidents, connection, lastSync } = useIncidents();
  const liveEdges = useIncidentEdges();
  const [selected, setSelected] = useState<Incident | null>(null);

  // Only ever substitute demo data when the backend is actually
  // unreachable — a healthy backend reporting zero incidents renders its
  // real (correct) empty state instead.
  const isDemo = connection === "error" && incidents.length === 0;
  const displayIncidents = isDemo ? DEMO_INCIDENTS : incidents;
  const displayEdges = isDemo ? DEMO_EDGES : liveEdges;

  const selectedIncident = useMemo(
    () => displayIncidents.find((i) => i.incident_id === selected?.incident_id) ?? null,
    [displayIncidents, selected],
  );

  return (
    <div className="flex flex-col h-screen">
      <header className="flex items-center justify-between px-4 py-3 border-b border-(--hairline)">
        <div>
          <h1 className="text-sm font-medium tracking-wide">Alert Fatigue Buster</h1>
          <p className="text-[11px] text-(--text-faint)">
            Incident console: dedup, burst suppression, and root-cause correlation
          </p>
        </div>
        <div className="flex items-center gap-3">
          {isDemo && (
            <span className="text-[11px] font-(family-name:--font-data) uppercase tracking-wider text-(--warn) border border-(--warn) px-2 py-0.5">
              Demo data
            </span>
          )}
          <ConnectionStatus state={connection} lastSync={lastSync} />
        </div>
      </header>

      <main className="flex-1 flex min-h-0">
        <div className="flex-1 flex flex-col gap-3 p-4 min-h-0">
          <VolumePanel incidents={displayIncidents} />
          <IncidentBoard incidents={displayIncidents} onSelect={setSelected} />
          <RootCauseGraph incidents={displayIncidents} edges={displayEdges} />
        </div>

        {selectedIncident && (
          <IncidentDrawer incident={selectedIncident} onClose={() => setSelected(null)} />
        )}
      </main>
    </div>
  );
}
