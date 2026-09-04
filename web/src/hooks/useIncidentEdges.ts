"use client";

import { useEffect, useRef, useState } from "react";
import { fetchIncidentEdges } from "@/lib/api";
import type { IncidentEdge } from "@/lib/types";

const POLL_INTERVAL_MS = 6000;

export function useIncidentEdges() {
  const [edges, setEdges] = useState<IncidentEdge[]>([]);
  const mountedRef = useRef(true);

  useEffect(() => {
    mountedRef.current = true;
    const poll = async () => {
      const batch = await fetchIncidentEdges();
      if (mountedRef.current && batch.length > 0) setEdges(batch);
    };
    poll();
    const id = setInterval(poll, POLL_INTERVAL_MS);
    return () => {
      mountedRef.current = false;
      clearInterval(id);
    };
  }, []);

  return edges;
}
