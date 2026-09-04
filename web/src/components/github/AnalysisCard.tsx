"use client";

import { useState } from "react";
import { DiffView } from "./DiffView";
import { createPatchPreview, PulseGraphApiError } from "@/lib/api";
import type { DiagnosisFallbackReason, GithubAnalysis, GithubPatchPreview } from "@/lib/types";

const FALLBACK_REASON: Record<DiagnosisFallbackReason, string> = {
  no_source_excerpts: "No bounded source excerpts",
  provider_unavailable: "Diagnosis provider unavailable",
  invalid_provider_result: "Result was not grounded in the snapshot",
  insufficient_evidence: "Insufficient evidence",
};

function Label({ children }: { children: React.ReactNode }) {
  return (
    <p className="text-[11px] font-medium uppercase tracking-wide text-text-3">{children}</p>
  );
}

function shortSha(sha: string | null): string {
  return sha ? sha.slice(0, 7) : "unknown";
}

/**
 * One saved analysis, and the patch preview it can produce.
 *
 * A fallback is rendered as a first-class outcome rather than an error: it is
 * what the backend returns instead of an unverified root-cause claim, and it
 * still carries next steps.
 */
export function AnalysisCard({ analysis }: { analysis: GithubAnalysis }) {
  const [patch, setPatch] = useState<GithubPatchPreview | null>(null);
  const [patchState, setPatchState] = useState<"idle" | "loading" | "error">("idle");
  const [patchError, setPatchError] = useState<string | null>(null);

  const { diagnosis } = analysis;
  const isDiagnosed = diagnosis.status === "diagnosed";
  const sourceEvidence = diagnosis.evidence.filter((item) => item.kind === "source_excerpt");
  const incidentEvidence = diagnosis.evidence.filter((item) => item.kind === "incident");

  const requestPatch = async () => {
    setPatchState("loading");
    setPatchError(null);
    try {
      setPatch(await createPatchPreview(analysis.analysis_id));
      setPatchState("idle");
    } catch (error) {
      setPatchState("error");
      setPatchError(
        error instanceof PulseGraphApiError
          ? error.message
          : "The patch preview could not be created.",
      );
    }
  };

  return (
    <article className="rounded-md border border-edge">
      <header className="flex flex-wrap items-center justify-between gap-2 border-b border-edge px-4 py-2.5">
        <div className="flex flex-wrap items-center gap-2">
          <span
            className={`inline-flex rounded px-2 py-0.5 text-[12px] font-medium ${
              isDiagnosed ? "bg-[#EFF4FF] text-[#1D4ED8]" : "bg-[#FFFBEB] text-[#B45309]"
            }`}
          >
            {isDiagnosed ? "Grounded diagnosis" : "Safe fallback"}
          </span>
          <span className="font-mono text-[12px] text-text-2">{diagnosis.provider}</span>
          {isDiagnosed && (
            <span className="font-mono text-[12px] tabular-nums text-text-2">
              {Math.round(diagnosis.confidence * 100)}% confidence
            </span>
          )}
        </div>
        <time className="font-mono text-[12px] text-text-3" dateTime={analysis.created_at}>
          {analysis.created_at}
        </time>
      </header>

      <div className="space-y-4 px-4 py-4">
        {diagnosis.status === "fallback" && diagnosis.fallback && (
          <>
            <div>
              <Label>Why there is no diagnosis</Label>
              <p className="mt-1 text-[13px] font-medium text-text">
                {FALLBACK_REASON[diagnosis.fallback.reason]}
              </p>
              <p className="mt-1 text-[13px] text-text-2">{diagnosis.fallback.message}</p>
            </div>
            <div>
              <Label>Next steps</Label>
              <ul className="mt-1.5 list-disc space-y-1 pl-5 text-[13px] text-text-2">
                {diagnosis.fallback.next_steps.map((step) => (
                  <li key={step}>{step}</li>
                ))}
              </ul>
            </div>
          </>
        )}

        {diagnosis.root_cause_hypothesis && (
          <div>
            <Label>Root cause hypothesis</Label>
            <p className="mt-1 text-[13px] font-medium text-text">
              {diagnosis.root_cause_hypothesis.summary}
            </p>
            <p className="mt-1 text-[13px] leading-relaxed text-text-2">
              {diagnosis.root_cause_hypothesis.reasoning}
            </p>
          </div>
        )}

        {sourceEvidence.length > 0 && (
          <div>
            <Label>Source evidence</Label>
            <ul className="mt-1.5 space-y-2">
              {sourceEvidence.map((item, index) => (
                <li key={`${item.file_path}-${index}`} className="rounded border border-edge bg-panel-2 px-3 py-2">
                  <p className="font-mono text-[12px] text-text">
                    {item.file_path}
                    <span className="text-text-2">
                      :{item.start_line}–{item.end_line}
                    </span>
                    <span className="ml-2 text-text-3">blob {shortSha(item.blob_sha)}</span>
                  </p>
                  <p className="mt-1 text-[13px] text-text-2">{item.explanation}</p>
                </li>
              ))}
            </ul>
          </div>
        )}

        {incidentEvidence.length > 0 && (
          <div>
            <Label>Incident evidence</Label>
            <ul className="mt-1.5 list-disc space-y-1 pl-5 text-[13px] text-text-2">
              {incidentEvidence.map((item, index) => (
                <li key={index}>{item.explanation}</li>
              ))}
            </ul>
          </div>
        )}

        {diagnosis.proposed_fix && (
          <div>
            <Label>Proposed fix</Label>
            <p className="mt-1 text-[13px] font-medium text-text">
              {diagnosis.proposed_fix.summary}
            </p>
            <ol className="mt-1.5 list-decimal space-y-1 pl-5 text-[13px] text-text-2">
              {diagnosis.proposed_fix.steps.map((step, index) => (
                <li key={index}>{step}</li>
              ))}
            </ol>
            {diagnosis.proposed_fix.affected_paths.length > 0 && (
              <p className="mt-2 flex flex-wrap gap-1.5">
                {diagnosis.proposed_fix.affected_paths.map((path) => (
                  <span
                    key={path}
                    className="rounded bg-panel-2 px-2 py-0.5 font-mono text-[12px] text-text-2"
                  >
                    {path}
                  </span>
                ))}
              </p>
            )}
            <p className="mt-2 text-[12px] text-[#B45309]">
              Requires human review. PulseGraph does not apply this.
            </p>
          </div>
        )}

        <p className="text-[12px] text-text-3">
          Snapshot <span className="font-mono">{analysis.snapshot_id.slice(0, 8)}</span> ·{" "}
          {analysis.source_context.excerpt_count} excerpts ·{" "}
          {analysis.source_context.byte_count.toLocaleString()} bytes of source read
        </p>

        {isDiagnosed && diagnosis.proposed_fix && (
          <div className="border-t border-edge pt-3">
            <div className="flex flex-wrap items-center gap-3">
              <button
                type="button"
                onClick={requestPatch}
                disabled={patchState === "loading"}
                className="rounded-md border border-edge bg-panel px-3 py-1.5 text-[13px] font-medium text-text transition-colors hover:bg-panel-2 disabled:cursor-wait disabled:text-text-3"
              >
                {patchState === "loading"
                  ? "Building preview…"
                  : patch
                    ? "Rebuild patch preview"
                    : "Generate patch preview"}
              </button>
              <p className="text-[12px] text-text-2">
                Produces a diff to read. Nothing is written to GitHub or to your working copy.
              </p>
            </div>

            {patchState === "error" && patchError && (
              <p role="alert" className="mt-2 text-[12px] text-[#B91C1C]">
                {patchError}
              </p>
            )}

            {patch && (
              <div className="mt-3 space-y-2">
                <div>
                  <p className="text-[13px] font-medium text-text">{patch.patch.summary}</p>
                  {patch.patch.rationale && (
                    <p className="mt-1 text-[13px] text-text-2">{patch.patch.rationale}</p>
                  )}
                </div>
                <ul className="flex flex-wrap gap-1.5">
                  {patch.patch.changed_files.map((file) => (
                    <li
                      key={file.path}
                      className="rounded bg-panel-2 px-2 py-0.5 font-mono text-[12px] text-text-2"
                    >
                      {file.action} {file.path}
                    </li>
                  ))}
                </ul>
                <DiffView diff={patch.patch.unified_diff} />
              </div>
            )}
          </div>
        )}
      </div>
    </article>
  );
}
