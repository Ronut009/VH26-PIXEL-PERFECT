import type { Incident } from "@/lib/types";

function Stat({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <div className="border border-(--hairline) bg-(--bg-raised) px-4 py-3 flex flex-col gap-1">
      <span className="text-[11px] font-(family-name:--font-data) uppercase tracking-wider text-(--text-dim)">
        {label}
      </span>
      <span className="font-(family-name:--font-data) text-2xl tabular-nums">{value}</span>
      {hint && <span className="text-[11px] text-(--text-faint)">{hint}</span>}
    </div>
  );
}

export function VolumePanel({ incidents }: { incidents: Incident[] }) {
  const rawSignals = incidents.reduce((sum, i) => sum + i.alert_count, 0);
  const surfaced = incidents.length;
  const suppressed = Math.max(0, rawSignals - surfaced);
  const cutPct = rawSignals > 0 ? Math.round((suppressed / rawSignals) * 100) : 0;

  return (
    <div className="grid grid-cols-3 gap-3">
      <Stat label="Raw signals in" value={rawSignals.toLocaleString()} hint="alerts received" />
      <Stat label="Incidents surfaced" value={surfaced.toLocaleString()} hint="after dedupe + correlation" />
      <Stat
        label="Noise cut"
        value={`${cutPct}%`}
        hint={`${suppressed.toLocaleString()} alerts folded in`}
      />
    </div>
  );
}
