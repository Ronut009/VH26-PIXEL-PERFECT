"use client";

import { useEffect, useState } from "react";
import { fetchHealth, PulseGraphApiError } from "@/lib/api";

const POLL_INTERVAL_MS = 15_000;

export type HealthState = "unknown" | "healthy" | "degraded" | "unreachable";

export interface BackendHealth {
  state: HealthState;
  /** True once the backend has answered at all, whatever it said. */
  apiReachable: boolean;
  /** True only when `/v1/health` reports its writer connection is usable. */
  databaseHealthy: boolean;
  /** What went wrong, and the next step, when something did. */
  message: string | null;
  action: string | null;
}

const UNKNOWN: BackendHealth = {
  state: "unknown",
  apiReachable: false,
  databaseHealthy: false,
  message: null,
  action: null,
};

async function probeOnce(): Promise<BackendHealth> {
  try {
    const report = await fetchHealth();
    if (report.status === "healthy") {
      return {
        state: "healthy",
        apiReachable: true,
        databaseHealthy: true,
        message: null,
        action: null,
      };
    }
    return {
      state: "degraded",
      apiReachable: true,
      databaseHealthy: false,
      message: report.error
        ? `The backend is running but its database is unhealthy: ${report.error}`
        : "The backend is running but reports an unhealthy database.",
      action: "Check the SQLite file at DATABASE_PATH, then run scripts/init_db.py.",
    };
  } catch (error) {
    return {
      state: "unreachable",
      apiReachable: false,
      databaseHealthy: false,
      message:
        error instanceof PulseGraphApiError ? error.message : "The backend health check failed.",
      action: "Start it with `uvicorn src.main:app --reload`, then check PULSEGRAPH_API_BASE.",
    };
  }
}

/**
 * Real liveness, from `GET /api/health`.
 *
 * The stream connection alone cannot tell these apart: a reachable API whose
 * SQLite writer is broken looks identical to a healthy one until you ask. This
 * asks, on a slow cadence, so the system-health readout reports what the
 * backend actually said rather than inferring it from the socket.
 */
export function useBackendHealth(): BackendHealth {
  const [health, setHealth] = useState<BackendHealth>(UNKNOWN);

  useEffect(() => {
    let cancelled = false;

    const probe = async () => {
      const next = await probeOnce();
      if (!cancelled) setHealth(next);
    };

    void probe();
    const id = setInterval(() => void probe(), POLL_INTERVAL_MS);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, []);

  return health;
}
