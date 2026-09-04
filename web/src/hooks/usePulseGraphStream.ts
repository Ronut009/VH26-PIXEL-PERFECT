"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { fetchIncidentsSince, streamUrl } from "@/lib/api";
import { normalizeEdge, normalizeIncident } from "@/lib/types";
import type { Incident, IncidentEdge } from "@/lib/types";

const POLL_INTERVAL_MS = 4000;

export type StreamState = "connecting" | "live" | "polling" | "offline";

type SnapshotPayload = {
  streamId?: number;
  incidents?: Record<string, unknown>[];
  edges?: Record<string, unknown>[];
};

/**
 * Consumes GET /v1/stream through the same-origin proxy at /api/stream.
 *
 * The subscription is same-origin because EventSource sends no CORS
 * preflight and the backend sets no CORS headers — pointed at the backend
 * directly it would simply never open, and silently fall through to the
 * polling path below.
 *
 * The backend assigns every event a monotonic `streamId` (see
 * src/stream/sse_broker.py), so reconnects resume with `?after=<id>` and no
 * events are replayed or skipped. EventSource cannot set the Last-Event-ID
 * header itself on a fresh connection, so the cursor travels in the query
 * string, which that route accepts as an equivalent.
 *
 * If the stream cannot be established the hook degrades to REST polling
 * against /v1/incidents/recent rather than leaving the dashboard blank.
 */
export function usePulseGraphStream() {
  const [incidents, setIncidents] = useState<Map<string, Incident>>(new Map());
  const [edges, setEdges] = useState<Map<string, IncidentEdge>>(new Map());
  const [state, setState] = useState<StreamState>("connecting");
  const [lastUpdated, setLastUpdated] = useState<Date | null>(null);
  const [loading, setLoading] = useState(true);

  const cursorRef = useRef<number>(0);
  const restCursorRef = useRef<string | undefined>(undefined);
  const mountedRef = useRef(true);

  const mergeIncidents = useCallback((rows: Record<string, unknown>[]) => {
    if (rows.length === 0) return;
    setIncidents((prev) => {
      const next = new Map(prev);
      for (const row of rows) {
        const incident = normalizeIncident(row);
        if (incident.incident_id) next.set(incident.incident_id, incident);
      }
      return next;
    });
  }, []);

  const mergeEdges = useCallback((rows: Record<string, unknown>[]) => {
    if (rows.length === 0) return;
    setEdges((prev) => {
      const next = new Map(prev);
      for (const row of rows) {
        const edge = normalizeEdge(row);
        if (edge.src_incident_id && edge.dst_incident_id) {
          next.set(`${edge.src_incident_id}->${edge.dst_incident_id}`, edge);
        }
      }
      return next;
    });
  }, []);

  // REST fallback, used only when the stream cannot be established.
  const pollOnce = useCallback(async () => {
    try {
      const batch = await fetchIncidentsSince(restCursorRef.current);
      if (!mountedRef.current) return;
      for (const incident of batch) {
        if (!restCursorRef.current || incident.updated_at > restCursorRef.current) {
          restCursorRef.current = incident.updated_at;
        }
      }
      if (batch.length > 0) {
        setIncidents((prev) => {
          const next = new Map(prev);
          batch.forEach((incident) => next.set(incident.incident_id, incident));
          return next;
        });
      }
      setState("polling");
      setLastUpdated(new Date());
      setLoading(false);
    } catch {
      if (mountedRef.current) {
        setState("offline");
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    let source: EventSource | null = null;
    let pollTimer: ReturnType<typeof setInterval> | null = null;

    const startPolling = () => {
      if (pollTimer) return;
      pollOnce();
      pollTimer = setInterval(pollOnce, POLL_INTERVAL_MS);
    };

    const touch = (streamId: unknown) => {
      if (typeof streamId === "number" && streamId > cursorRef.current) {
        cursorRef.current = streamId;
      }
      setLastUpdated(new Date());
      setLoading(false);
      setState("live");
    };

    const parse = (event: MessageEvent): Record<string, unknown> | null => {
      try {
        return JSON.parse(event.data) as Record<string, unknown>;
      } catch {
        return null;
      }
    };

    try {
      source = new EventSource(streamUrl(cursorRef.current));

      // An open connection is itself the liveness signal. The backend only
      // emits its initial snapshot when `stream_id > cursor`, so against an
      // empty database the first thing to arrive is a keepalive comment — and
      // waiting for a data event would leave a perfectly healthy stream
      // reported as "Offline", stuck loading forever.
      source.onopen = () => {
        setState("live");
        setLoading(false);
        setLastUpdated(new Date());
      };

      source.addEventListener("snapshot", (event) => {
        const data = parse(event as MessageEvent) as SnapshotPayload | null;
        if (!data) return;
        mergeIncidents(data.incidents ?? []);
        mergeEdges(data.edges ?? []);
        touch(data.streamId);
      });

      source.addEventListener("incident.upsert", (event) => {
        const data = parse(event as MessageEvent);
        if (!data) return;
        if (data.incident) mergeIncidents([data.incident as Record<string, unknown>]);
        touch(data.streamId);
      });

      source.addEventListener("graph.edge.upsert", (event) => {
        const data = parse(event as MessageEvent);
        if (!data) return;
        mergeEdges((data.edges as Record<string, unknown>[]) ?? []);
        touch(data.streamId);
      });

      source.addEventListener("metrics.update", (event) => {
        const data = parse(event as MessageEvent);
        if (data) touch(data.streamId);
      });

      source.onerror = () => {
        // EventSource retries on its own; if it never opened, fall back to REST
        // so the dashboard still shows data.
        if (source && source.readyState === EventSource.CLOSED) {
          source.close();
          source = null;
        }
        startPolling();
      };
    } catch {
      startPolling();
    }

    return () => {
      mountedRef.current = false;
      if (source) source.close();
      if (pollTimer) clearInterval(pollTimer);
    };
  }, [mergeIncidents, mergeEdges, pollOnce]);

  return {
    incidents: Array.from(incidents.values()),
    edges: Array.from(edges.values()),
    state,
    lastUpdated,
    loading,
  };
}
