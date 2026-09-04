"use client";

export type ViewKey =
  | "overview"
  | "incidents"
  | "correlations"
  | "deliveries"
  | "audit"
  | "github"
  | "settings";

const S = {
  width: 16,
  height: 16,
  viewBox: "0 0 16 16",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 1.4,
  strokeLinecap: "round" as const,
  strokeLinejoin: "round" as const,
};

function IconGrid() {
  return (
    <svg {...S}>
      <rect x="2" y="2" width="5" height="5" rx="1" />
      <rect x="9" y="2" width="5" height="5" rx="1" />
      <rect x="2" y="9" width="5" height="5" rx="1" />
      <rect x="9" y="9" width="5" height="5" rx="1" />
    </svg>
  );
}
function IconList() {
  return (
    <svg {...S}>
      <path d="M5 4h9M5 8h9M5 12h9M2.5 4h.01M2.5 8h.01M2.5 12h.01" />
    </svg>
  );
}
function IconGraph() {
  return (
    <svg {...S}>
      <circle cx="8" cy="3.5" r="1.8" />
      <circle cx="3.5" cy="12" r="1.8" />
      <circle cx="12.5" cy="12" r="1.8" />
      <path d="M7 5.2 4.6 10.3M9 5.2l2.4 5.1" />
    </svg>
  );
}
function IconSend() {
  return (
    <svg {...S}>
      <path d="m14 2-6 12-2-5-5-2 13-5Z" />
    </svg>
  );
}
function IconLedger() {
  return (
    <svg {...S}>
      <rect x="3" y="2" width="10" height="12" rx="1.5" />
      <path d="M6 5.5h4M6 8h4M6 10.5h2.5" />
    </svg>
  );
}
function IconCode() {
  return (
    <svg {...S}>
      <path d="m6 5.5-3.5 2.5 3.5 2.5M10 5.5 13.5 8 10 10.5M9.2 3.2 6.8 12.8" />
    </svg>
  );
}
function IconCog() {
  return (
    <svg {...S}>
      <circle cx="8" cy="8" r="2.2" />
      <path d="M8 1.8v1.6M8 12.6v1.6M14.2 8h-1.6M3.4 8H1.8M12.4 3.6l-1.1 1.1M4.7 11.3l-1.1 1.1M12.4 12.4l-1.1-1.1M4.7 4.7 3.6 3.6" />
    </svg>
  );
}

const NAV: { key: ViewKey; label: string; icon: React.ReactNode }[] = [
  { key: "overview", label: "Overview", icon: <IconGrid /> },
  { key: "incidents", label: "Incidents", icon: <IconList /> },
  { key: "correlations", label: "Correlations", icon: <IconGraph /> },
  { key: "deliveries", label: "Deliveries", icon: <IconSend /> },
  { key: "audit", label: "Audit Ledger", icon: <IconLedger /> },
  { key: "github", label: "Code Investigation", icon: <IconCode /> },
  { key: "settings", label: "Settings", icon: <IconCog /> },
];

const HEALTH = [
  { label: "API", key: "api" },
  { label: "SSE Stream", key: "sse" },
  { label: "Database", key: "db" },
  // The outbox worker exposes no health endpoint, so its row reports
  // "Unknown" rather than asserting a state inferred from the socket.
  { label: "Outbox Worker", key: "outbox" },
] as const;

export function Sidebar({
  view,
  onView,
  connected,
  health,
}: {
  view: ViewKey;
  onView: (next: ViewKey) => void;
  connected: boolean;
  /** `null` means no endpoint reports this, which is not the same as "down". */
  health: Record<string, boolean | null>;
}) {
  return (
    <aside className="hidden w-60 shrink-0 flex-col border-r border-edge bg-panel lg:flex">
      <div className="flex items-center gap-2 px-5 py-5">
        <svg width="20" height="20" viewBox="0 0 20 20" fill="none" aria-hidden>
          <path
            d="M1 10h3.2l2-5.2 3 11L12 8.2l1.4 1.8H19"
            stroke="#C8FF3D"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
        <span className="text-[15px] font-semibold tracking-tight text-text">PulseGraph</span>
      </div>

      <nav className="flex flex-col gap-0.5 px-3">
        {NAV.map((item) => {
          const active = view === item.key;
          return (
            <button
              key={item.key}
              type="button"
              onClick={() => onView(item.key)}
              aria-current={active ? "page" : undefined}
              className={`flex items-center gap-2.5 rounded-md px-3 py-2 text-left text-[13px] transition-colors ${
                active
                  ? "bg-brand-soft font-medium text-brand"
                  : "text-text-2 hover:bg-panel-2 hover:text-text"
              }`}
            >
              <span className={active ? "text-brand" : "text-text-3"}>{item.icon}</span>
              {item.label}
            </button>
          );
        })}
      </nav>

      <div className="mt-auto px-3 pb-4">
        <div className="rounded-md border border-edge bg-panel-2 p-3">
          <p className="text-[11px] font-medium uppercase tracking-wide text-text-3">
            System health
          </p>
          <ul className="mt-2.5 space-y-1.5">
            {HEALTH.map((row) => {
              const ok = health[row.key] ?? null;
              const dot = ok === null ? "bg-text-3" : ok ? "bg-ok" : "bg-crit";
              const tone =
                ok === null ? "text-text-3" : ok ? "text-ok" : "text-crit";
              return (
                <li key={row.key} className="flex items-center justify-between text-[12px]">
                  <span className="text-text-2">{row.label}</span>
                  <span className="flex items-center gap-1.5">
                    <span className={`size-1.5 rounded-full ${dot}`} aria-hidden />
                    <span className={tone}>
                      {ok === null ? "Unknown" : ok ? "Healthy" : "Down"}
                    </span>
                  </span>
                </li>
              );
            })}
          </ul>
        </div>

        <div className="mt-2 flex items-center justify-between px-2 text-[12px]">
          <span className="flex items-center gap-1.5 text-text-2">
            <span
              className={`size-1.5 rounded-full ${connected ? "bg-ok live-pulse" : "bg-text-3"}`}
              aria-hidden
            />
            Live
          </span>
          <span className={connected ? "text-ok" : "text-text-3"}>
            {connected ? "Connected" : "Offline"}
          </span>
        </div>
      </div>
    </aside>
  );
}

/**
 * Navigation for the widths where the rail is hidden.
 *
 * Without this the whole console is unreachable below `lg` — every view but
 * the default one has no way in on a phone or a portrait tablet.
 */
export function MobileNav({
  view,
  onView,
}: {
  view: ViewKey;
  onView: (next: ViewKey) => void;
}) {
  return (
    <nav
      aria-label="Console sections"
      className="flex gap-1 overflow-x-auto border-b border-edge bg-panel px-3 py-2 lg:hidden"
    >
      {NAV.map((item) => {
        const active = view === item.key;
        return (
          <button
            key={item.key}
            type="button"
            onClick={() => onView(item.key)}
            aria-current={active ? "page" : undefined}
            className={`flex shrink-0 items-center gap-2 rounded-md px-3 py-1.5 text-[13px] transition-colors ${
              active
                ? "bg-brand-soft font-medium text-brand"
                : "text-text-2 hover:bg-panel-2 hover:text-text"
            }`}
          >
            <span className={active ? "text-brand" : "text-text-3"}>{item.icon}</span>
            {item.label}
          </button>
        );
      })}
    </nav>
  );
}
