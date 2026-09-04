"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchIncidentsSince } from "@/lib/api";
import type { Incident } from "@/lib/types";

const POLL_INTERVAL_MS = 4000;

export type ConnectionState = "connecting" | "live" | "error";

/**
 * Everything below the returned interface polls GET /v1/incidents/recent.
 * When Anish's SSE publisher (GET /v1/stream) ships, replace the body of
 * `sync()` with an EventSource that pushes deltas into the same
 * `incidentsRef` map — `incidents` / `connection` / `lastSync` stay the
 * same shape for every component that consumes this hook.
 */
export function useIncidents() {
  const [incidents, setIncidents] = useState<Map<string, Incident>>(new Map());
  const [connection, setConnection] = useState<ConnectionState>("connecting");
  const [lastSync, setLastSync] = useState<Date | null>(null);
  const cursorRef = useRef<string | undefined>(undefined);
  const mountedRef = useRef(true);

  const sync = useCallback(async () => {
    try {
      const batch = await fetchIncidentsSince(cursorRef.current);
      if (!mountedRef.current) return;

      if (batch.length > 0) {
        setIncidents((prev) => {
          const next = new Map(prev);
          for (const incident of batch) {
            next.set(incident.incident_id, incident);
            if (!cursorRef.current || incident.updated_at > cursorRef.current) {
              cursorRef.current = incident.updated_at;
            }
          }
          return next;
        });
      }
      setConnection("live");
      setLastSync(new Date());
    } catch {
      if (mountedRef.current) setConnection("error");
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    sync();
    const id = setInterval(sync, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, [sync]);

  return {
    incidents: Array.from(incidents.values()),
    connection,
    lastSync,
  };
}
