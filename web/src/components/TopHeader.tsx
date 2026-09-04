"use client";

import { useEffect, useState } from "react";

export function TopHeader({
  title,
  connected,
  lastEvent,
}: {
  title: string;
  connected: boolean;
  lastEvent: Date | null;
}) {
  const [now, setNow] = useState<string>("");
  const [ago, setAgo] = useState<string | null>(null);

  useEffect(() => {
    const tick = () => {
      setNow(new Date().toLocaleTimeString("en-GB", { hour12: false, timeZone: "UTC" }));
      if (!lastEvent) {
        setAgo(null);
        return;
      }
      const seconds = Math.max(0, Math.round((Date.now() - lastEvent.getTime()) / 1000));
      setAgo(seconds < 60 ? `${seconds}s ago` : `${Math.round(seconds / 60)}m ago`);
    };
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  }, [lastEvent]);

  return (
    <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b border-edge bg-panel px-6">
      <div className="flex items-baseline gap-2 text-[13px]">
        <span className="font-medium text-text">{title}</span>
        <span className="text-text-3">/</span>
        <span className="text-text-3">Dashboard</span>
      </div>

      <div className="flex items-center gap-4 text-[12px]">
        <span className="flex items-center gap-1.5">
          <span
            className={`size-1.5 rounded-full ${connected ? "bg-ok live-pulse" : "bg-crit"}`}
            aria-hidden
          />
          <span className={connected ? "text-[#15803D]" : "text-[#B91C1C]"}>
            {connected ? "Live" : "Offline"}
          </span>
        </span>

        {ago && <span className="hidden text-text-2 sm:inline">Last event {ago}</span>}
        {now && (
          <span className="hidden font-mono tabular-nums text-text-2 md:inline">{now} UTC</span>
        )}

        <button
          type="button"
          aria-label="Notifications"
          className="rounded p-1 text-text-3 transition-colors hover:bg-panel-2 hover:text-text-2"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 16 16"
            fill="none"
            stroke="currentColor"
            strokeWidth="1.4"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="M8 2a4 4 0 0 0-4 4c0 3-1 4-1 4h10s-1-1-1-4a4 4 0 0 0-4-4Z" />
            <path d="M6.8 12.5a1.5 1.5 0 0 0 2.4 0" />
          </svg>
        </button>

        <span
          className="grid size-7 place-items-center rounded-full bg-brand text-[11px] font-medium text-white"
          aria-label="Signed in"
        >
          PG
        </span>
      </div>
    </header>
  );
}
