export function relativeTime(iso: string): string {
  const then = new Date(iso.endsWith("Z") ? iso : `${iso}Z`).getTime();
  const diffSec = Math.max(0, Math.round((Date.now() - then) / 1000));

  if (diffSec < 5) return "just now";
  if (diffSec < 60) return `${diffSec}s ago`;
  const diffMin = Math.round(diffSec / 60);
  if (diffMin < 60) return `${diffMin}m ago`;
  const diffHr = Math.round(diffMin / 60);
  if (diffHr < 24) return `${diffHr}h ago`;
  const diffDay = Math.round(diffHr / 24);
  return `${diffDay}d ago`;
}

export function clockTime(iso: string): string {
  const d = new Date(iso.endsWith("Z") ? iso : `${iso}Z`);
  return d.toLocaleTimeString(undefined, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}
