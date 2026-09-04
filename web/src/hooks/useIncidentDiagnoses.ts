"use client";

import { useCallback, useEffect, useState } from "react";
import { createIncidentDiagnosis, fetchIncidentDiagnoses, PulseGraphApiError } from "@/lib/api";
import type { GithubAnalysis } from "@/lib/types";

export type DiagnosesState = "loading" | "ready" | "error";

/**
 * The saved analyses for one incident, and the ability to add another.
 *
 * Mount this per incident (a `key` on the consuming component is enough) so
 * switching incidents resets cleanly rather than briefly showing the previous
 * incident's analyses.
 */
export function useIncidentDiagnoses(incidentId: string) {
  const [analyses, setAnalyses] = useState<GithubAnalysis[]>([]);
  const [state, setState] = useState<DiagnosesState>("loading");
  const [error, setError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [running, setRunning] = useState(false);
  const [runError, setRunError] = useState<string | null>(null);

  const reload = useCallback(() => {
    setState("loading");
    setError(null);
    setAttempt((count) => count + 1);
  }, []);

  useEffect(() => {
    let cancelled = false;

    const load = async () => {
      try {
        const records = await fetchIncidentDiagnoses(incidentId);
        if (cancelled) return;
        setAnalyses(records);
        setState("ready");
      } catch (caught) {
        if (cancelled) return;
        setError(
          caught instanceof PulseGraphApiError
            ? caught.message
            : "The saved analyses could not be read.",
        );
        setState("error");
      }
    };

    void load();
    return () => {
      cancelled = true;
    };
  }, [incidentId, attempt]);

  /**
   * Ask the backend for one bounded diagnosis. A safe fallback is a successful
   * result, not an error — it gets prepended like any other analysis.
   */
  const runDiagnosis = useCallback(async () => {
    setRunning(true);
    setRunError(null);
    try {
      const created = await createIncidentDiagnosis(incidentId);
      setAnalyses((previous) => [created, ...previous]);
    } catch (caught) {
      setRunError(
        caught instanceof PulseGraphApiError
          ? caught.message
          : "The diagnosis could not be created.",
      );
    } finally {
      setRunning(false);
    }
  }, [incidentId]);

  return { analyses, state, error, reload, runDiagnosis, running, runError };
}
