"use client";

import { useMemo, useState } from "react";

type LineKind = "add" | "del" | "hunk" | "meta" | "context";

const LINE_STYLE: Record<LineKind, string> = {
  add: "bg-[#F0FDF4] text-[#15803D]",
  del: "bg-[#FEF2F2] text-[#B91C1C]",
  hunk: "bg-brand-soft text-[#1D4ED8]",
  meta: "text-text-3",
  context: "text-text-2",
};

function classify(line: string): LineKind {
  if (line.startsWith("@@")) return "hunk";
  if (line.startsWith("+++") || line.startsWith("---")) return "meta";
  if (line.startsWith("diff ") || line.startsWith("index ") || line.startsWith("new file")) {
    return "meta";
  }
  if (line.startsWith("+")) return "add";
  if (line.startsWith("-")) return "del";
  return "context";
}

/**
 * Renders a unified diff for reading.
 *
 * This is a viewer and nothing else. There is no control here — and no
 * endpoint behind it — that applies the patch, commits it, or sends it
 * anywhere: the backend builds the diff in a temporary workspace and throws
 * that workspace away before responding.
 */
export function DiffView({ diff }: { diff: string }) {
  const [copied, setCopied] = useState(false);
  const [copyFailed, setCopyFailed] = useState(false);

  const lines = useMemo(() => diff.replace(/\n$/, "").split("\n"), [diff]);
  const added = lines.filter((line) => classify(line) === "add").length;
  const removed = lines.filter((line) => classify(line) === "del").length;

  const copy = async () => {
    setCopyFailed(false);
    try {
      await navigator.clipboard.writeText(diff);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard access is denied in some browsers and contexts; say so
      // rather than showing a button that silently does nothing.
      setCopyFailed(true);
    }
  };

  return (
    <div className="rounded-md border border-edge">
      <div className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-3 py-2">
        <p className="text-[12px] text-text-2">
          <span className="font-mono tabular-nums text-[#15803D]">+{added}</span>{" "}
          <span className="font-mono tabular-nums text-[#B91C1C]">−{removed}</span>{" "}
          <span className="ml-1">unified diff, for review only</span>
        </p>
        <div className="flex items-center gap-2">
          {copyFailed && (
            <span role="status" className="text-[12px] text-[#B45309]">
              Clipboard unavailable — select the diff to copy it.
            </span>
          )}
          <button
            type="button"
            onClick={copy}
            className="rounded-md border border-edge px-2.5 py-1 text-[12px] text-text-2 transition-colors hover:bg-panel-2 hover:text-text"
          >
            {copied ? "Copied" : "Copy diff"}
          </button>
        </div>
      </div>

      <div className="overflow-x-auto">
        <pre className="min-w-full py-1 font-mono text-[12px] leading-[1.6]">
          {lines.map((line, index) => {
            const kind = classify(line);
            return (
              <code
                key={index}
                className={`block whitespace-pre px-3 ${LINE_STYLE[kind]}`}
              >
                {line === "" ? " " : line}
              </code>
            );
          })}
        </pre>
      </div>
    </div>
  );
}
