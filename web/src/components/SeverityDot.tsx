import { SEVERITY_COLOR } from "@/lib/theme";
import type { Severity } from "@/lib/types";

export function SeverityDot({ severity }: { severity: Severity }) {
  return (
    <span
      aria-hidden
      className="inline-block size-2 shrink-0 rounded-full"
      style={{ background: SEVERITY_COLOR[severity] }}
    />
  );
}
