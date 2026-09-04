"use client";

import { useEffect, useState } from "react";
import { relativeTime } from "@/lib/format";
import { SEVERITY } from "@/lib/theme";
import { consolidationSeries, quietWindow } from "@/lib/metrics";
import type { Incident } from "@/lib/types";

export function Panel({
  title,
  hint,
  action,
  children,
  className = "",
}: {
  title?: string;
  hint?: string;
  action?: React.ReactNode;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <section className={`rounded-lg border border-edge bg-panel ${className}`}>
      {(title || action) && (
        <header className="flex items-start justify-between gap-4 px-5 pb-3 pt-4">
          <div>
            {title && <h2 className="text-[14px] font-semibold text-text">{title}</h2>}
            {hint && <p className="mt-0.5 text-[12px] text-text-2">{hint}</p>}
          </div>
          {action}
        </header>
      )}
      <div className="px-5 pb-5">{children}</div>
    </section>
  );
}

/** Compact KPI tile. `emphasis` gives consolidation the extra weight it earns. */
export function Kpi({
  label,
  value,
  sub,
  tone = "text-text",
  emphasis = false,
}: {
  label: string;
  value: string;
  sub?: React.ReactNode;
  tone?: string;
  emphasis?: boolean;
}) {
  return (
    <div
      className={`rounded-lg border bg-panel px-5 py-4 ${
        emphasis ? "border-brand/30 ring-1 ring-brand/10" : "border-edge"
      }`}
    >
      <p className="text-[12px] font-medium text-text-2">{label}</p>
      <p className={`mt-1.5 font-mono text-[28px] font-semibold leading-none tabular-nums ${tone}`}>
        {value}
      </p>
      {sub && <div className="mt-2 text-[12px] text-text-2">{sub}</div>}
    </div>
  );
}

/**
 * Ingested versus consolidated. Both lines come from real incident records:
 * ingested is summed alert_count per window, consolidated is the number of
 * incidents those alerts became.
 */
export function AlertVolumeChart({ incidents }: { incidents: Incident[] }) {
  const series = consolidationSeries(incidents);
  if (series.length === 0) {
    return <p className="py-8 text-center text-[12px] text-text-2">No alert timestamps yet.</p>;
  }

  const w = 640;
  const h = 180;
  const pad = { top: 12, right: 8, bottom: 24, left: 34 };
  const maxIngested = Math.max(...series.map((s) => s.ingested), 1);
  const plotW = w - pad.left - pad.right;
  const plotH = h - pad.top - pad.bottom;
  const slot = plotW / series.length;
  const barW = Math.max(6, slot * 0.55);
  const cx = (i: number) => pad.left + slot * i + slot / 2;
  const y = (v: number) => pad.top + (1 - v / maxIngested) * plotH;

  // Ingested is a volume, so it reads as bars. Consolidated is a running count
  // and stays a line, which keeps the "many in, few out" comparison obvious.
  const consolidatedLine = series
    .map((s, i) => `${i === 0 ? "M" : "L"}${cx(i).toFixed(1)},${y(s.consolidated).toFixed(1)}`)
    .join(" ");
  const ticks = [0, Math.round(maxIngested / 2), maxIngested];

  return (
    <div>
      <div className="mb-3 flex items-center gap-4 text-[12px]">
        <span className="flex items-center gap-1.5 text-text-2">
          <span className="h-2.5 w-2.5 rounded-sm bg-brand" aria-hidden /> Ingested alerts
        </span>
        <span className="flex items-center gap-1.5 text-text-2">
          <span className="h-0.5 w-4 rounded-full bg-ok" aria-hidden /> Consolidated incidents
        </span>
      </div>

      <svg
        viewBox={`0 0 ${w} ${h}`}
        className="w-full"
        role="img"
        aria-label="Alert volume, ingested against consolidated"
      >
        {ticks.map((t) => (
          <g key={t}>
            <line
              x1={pad.left}
              x2={w - pad.right}
              y1={y(t)}
              y2={y(t)}
              stroke="#394135"
              strokeWidth="1"
            />
            <text x={4} y={y(t) + 3} fontSize="10" fill="#7F8A77">
              {t}
            </text>
          </g>
        ))}

        {series.map((s, i) => (
          <rect
            key={i}
            x={cx(i) - barW / 2}
            y={y(s.ingested)}
            width={barW}
            height={Math.max(1, pad.top + plotH - y(s.ingested))}
            rx="2"
            fill="#5DE4FF"
            opacity={0.85}
          >
            <title>{`${s.ingested} alerts ingested, consolidated into ${s.consolidated}`}</title>
          </rect>
        ))}

        <path
          d={consolidatedLine}
          fill="none"
          stroke="#C8FF3D"
          strokeWidth="1.75"
          strokeLinejoin="round"
        />
        {series.map((s, i) => (
          <circle key={`c${i}`} cx={cx(i)} cy={y(s.consolidated)} r="2.5" fill="#C8FF3D" />
        ))}
      </svg>
    </div>
  );
}

/** Adaptive quiet window, read from the engine's quiet_at_ms deadline. */
export function QuietWindow({ incidents }: { incidents: Incident[] }) {
  const window = quietWindow(incidents);

  if (!window) {
    return (
      <div className="py-6">
        <p className="text-[13px] text-text">No quiet window active</p>
        <p className="mt-1 text-[12px] text-text-2">
          The engine sets a deadline once a signal starts repeating.
        </p>
      </div>
    );
  }

  return <QuietWindowBody until={window.until} meanRate={window.meanRate} tracking={window.tracking} absorbed={window.absorbed} />;
}

function QuietWindowBody({
  until,
  meanRate,
  tracking,
  absorbed,
}: {
  until: Date;
  meanRate: number;
  tracking: number;
  absorbed: number;
}) {
  const [minsLeft, setMinsLeft] = useState<number | null>(null);

  useEffect(() => {
    const tick = () =>
      setMinsLeft(Math.max(0, Math.round((until.getTime() - Date.now()) / 60000)));
    tick();
    const id = setInterval(tick, 30000);
    return () => clearInterval(id);
  }, [until]);

  const progress =
    minsLeft === null ? 0 : Math.max(0, Math.min(100, 100 - (minsLeft / 30) * 100));

  return (
    <div>
      <p className="text-[12px] text-text-2">Quiet until</p>
      <p className="mt-1 font-mono text-[26px] font-semibold leading-none tabular-nums text-text">
        {until.toLocaleTimeString("en-GB", { hour12: false })}
      </p>

      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-panel-2">
        <div className="h-full rounded-full bg-brand" style={{ width: `${progress}%` }} />
      </div>
      <p className="mt-1.5 text-[12px] text-text-2">
        {minsLeft === null ? "calculating" : `${minsLeft} min`} remaining on the current deadline
      </p>

      <dl className="mt-4 space-y-2 border-t border-edge pt-3 text-[12px]">
        <div className="flex items-center justify-between">
          <dt className="text-text-2">Mean EWMA rate</dt>
          <dd className="font-mono tabular-nums text-text">{meanRate.toFixed(2)}/min</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-text-2">Signals tracked</dt>
          <dd className="font-mono tabular-nums text-text">{tracking}</dd>
        </div>
        <div className="flex items-center justify-between">
          <dt className="text-text-2">Duplicate events absorbed</dt>
          <dd className="font-mono tabular-nums text-text">{absorbed}</dd>
        </div>
      </dl>
    </div>
  );
}

export function RecentActivity({
  incidents,
  onSelect,
}: {
  incidents: Incident[];
  onSelect: (incident: Incident) => void;
}) {
  const rows = [...incidents]
    .sort((a, b) => b.updated_at.localeCompare(a.updated_at))
    .slice(0, 6);

  if (rows.length === 0) {
    return <p className="py-6 text-center text-[12px] text-text-2">No activity yet.</p>;
  }

  return (
    <ul className="divide-y divide-edge">
      {rows.map((incident) => {
        const severity = SEVERITY[incident.severity];
        return (
          <li key={incident.incident_id}>
            <button
              type="button"
              onClick={() => onSelect(incident)}
              className="flex w-full items-center gap-3 py-2.5 text-left transition-colors hover:bg-panel-2"
            >
              <span className={`size-1.5 shrink-0 rounded-full ${severity.dot}`} aria-hidden />
              <span className="min-w-0 flex-1 truncate text-[13px] text-text">
                {incident.title}
              </span>
              <span className="shrink-0 text-[12px] text-text-2">{severity.label}</span>
              <span className="w-16 shrink-0 text-right font-mono text-[12px] tabular-nums text-text-3">
                {incident.last_alert_at ? relativeTime(incident.last_alert_at) : ""}
              </span>
            </button>
          </li>
        );
      })}
    </ul>
  );
}
