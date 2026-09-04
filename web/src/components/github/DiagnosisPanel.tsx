"use client";

import { AnalysisCard } from "./AnalysisCard";
import { useIncidentDiagnoses } from "@/hooks/useIncidentDiagnoses";
import type { GithubRepository, Incident } from "@/lib/types";

/**
 * Run and review bounded diagnoses for one incident.
 *
 * "Run diagnosis" reads a small, capped set of files from the repository's
 * pinned snapshot and asks the configured provider to explain the incident
 * against them. It writes an analysis record to PulseGraph's own database and
 * touches nothing in GitHub.
 */
export function DiagnosisPanel({
  incident,
  repository,
}: {
  incident: Incident;
  repository: GithubRepository;
}) {
  const { analyses, state, error, reload, runDiagnosis, running, runError } = useIncidentDiagnoses(
    incident.incident_id,
  );

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="button"
          onClick={runDiagnosis}
          disabled={running}
          className="rounded-md bg-brand px-3 py-2 text-[13px] font-medium text-white transition-colors hover:bg-[#1D4ED8] disabled:cursor-wait disabled:bg-[#93B4FB]"
        >
          {running ? "Diagnosing…" : "Run diagnosis"}
        </button>
        <p className="text-[12px] text-text-2">
          Reads a bounded excerpt of{" "}
          <span className="font-mono text-text">{repository.full_name}</span> at its pinned commit.
          Read-only.
        </p>
      </div>

      {runError && (
        <p role="alert" className="rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2 text-[12px] text-[#B91C1C]">
          {runError}
        </p>
      )}

      {state === "loading" && (
        <p role="status" aria-busy className="text-[13px] text-text-2">
          Loading saved analyses…
        </p>
      )}

      {state === "error" && (
        <div className="rounded-md border border-[#FECACA] bg-[#FEF2F2] px-3 py-2">
          <p className="text-[13px] text-[#B91C1C]">{error}</p>
          <button
            type="button"
            onClick={reload}
            className="mt-2 rounded-md border border-edge bg-panel px-2.5 py-1 text-[12px] text-text transition-colors hover:bg-panel-2"
          >
            Try again
          </button>
        </div>
      )}

      {state === "ready" && analyses.length === 0 && (
        <p className="text-[13px] text-text-2">
          No diagnosis has been run for this incident yet.
        </p>
      )}

      {analyses.length > 0 && (
        <div className="space-y-3">
          {analyses.map((analysis) => (
            <AnalysisCard key={analysis.analysis_id} analysis={analysis} />
          ))}
        </div>
      )}
    </div>
  );
}
