import type { ConnectionState } from "@/hooks/useIncidents";

const COLOR: Record<ConnectionState, string> = {
  live: "var(--live)",
  connecting: "var(--warn)",
  error: "var(--down)",
};

const LABEL: Record<ConnectionState, string> = {
  live: "polling /v1/incidents/recent",
  connecting: "connecting",
  error: "backend unreachable",
};

export function ConnectionStatus({
  state,
  lastSync,
}: {
  state: ConnectionState;
  lastSync: Date | null;
}) {
  return (
    <div className="flex items-center gap-2 text-[11px] text-(--text-dim)">
      <span
        className="inline-block size-1.5 rounded-full"
        style={{ background: COLOR[state] }}
      />
      <span className="font-(family-name:--font-data) uppercase tracking-wide">{LABEL[state]}</span>
      {lastSync && (
        <span className="text-(--text-faint)">
          · synced {lastSync.toLocaleTimeString(undefined, { hour12: false })}
        </span>
      )}
    </div>
  );
}
