"use client";

import { useMemo } from "react";
import {
  ReactFlow,
  Background,
  BackgroundVariant,
  type Node,
  type Edge,
  MarkerType,
} from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import { SEVERITY_COLOR } from "@/lib/theme";
import type { Incident, IncidentEdge } from "@/lib/types";

const RADIUS = 160;

function layoutCircular(ids: string[]): Map<string, { x: number; y: number }> {
  const positions = new Map<string, { x: number; y: number }>();
  const n = Math.max(ids.length, 1);
  ids.forEach((id, i) => {
    const angle = (2 * Math.PI * i) / n - Math.PI / 2;
    positions.set(id, {
      x: RADIUS * Math.cos(angle) + RADIUS + 40,
      y: RADIUS * Math.sin(angle) + RADIUS + 40,
    });
  });
  return positions;
}

export function RootCauseGraph({
  incidents,
  edges,
}: {
  incidents: Incident[];
  edges: IncidentEdge[];
}) {
  const byId = useMemo(() => new Map(incidents.map((i) => [i.incident_id, i])), [incidents]);

  const { nodes, flowEdges, rootId } = useMemo(() => {
    const ids = Array.from(
      new Set(edges.flatMap((e) => [e.src_incident_id, e.dst_incident_id])),
    );
    const positions = layoutCircular(ids);

    const inbound = new Map<string, number>();
    for (const e of edges) inbound.set(e.dst_incident_id, (inbound.get(e.dst_incident_id) ?? 0) + e.weight);
    let rootId: string | null = null;
    let best = -Infinity;
    for (const [id, w] of inbound) if (w > best) { best = w; rootId = id; }

    const maxWeight = Math.max(1, ...edges.map((e) => e.weight));

    const nodes: Node[] = ids.map((id) => {
      const incident = byId.get(id);
      const isRoot = id === rootId;
      const color = incident ? SEVERITY_COLOR[incident.severity] : "var(--text-faint)";
      return {
        id,
        position: positions.get(id) ?? { x: 0, y: 0 },
        data: { label: incident?.title ?? id.slice(0, 8) },
        style: {
          background: "var(--bg-raised)",
          border: `1px solid ${isRoot ? color : "var(--hairline-strong)"}`,
          borderWidth: isRoot ? 2 : 1,
          borderRadius: 2,
          color: "var(--text)",
          fontFamily: "var(--font-ui)",
          fontSize: 12,
          padding: "6px 10px",
          width: 180,
        },
      };
    });

    const flowEdges: Edge[] = edges.map((e) => {
      const opacity = 0.25 + 0.75 * (e.weight / maxWeight);
      return {
        id: `${e.src_incident_id}->${e.dst_incident_id}`,
        source: e.src_incident_id,
        target: e.dst_incident_id,
        markerEnd: { type: MarkerType.ArrowClosed, color: "var(--text-dim)" },
        style: { stroke: "var(--text-dim)", strokeWidth: 1 + e.weight, opacity },
      };
    });

    return { nodes, flowEdges, rootId };
  }, [edges, byId]);

  if (edges.length === 0) {
    return (
      <div className="flex-1 flex items-center justify-center border border-(--hairline) bg-(--bg-raised) text-(--text-faint) text-xs px-4 text-center">
        No correlation data yet. This panel reads GET /v1/edges/recent, which
        is not wired up on the backend yet.
      </div>
    );
  }

  return (
    <div className="flex-1 border border-(--hairline) relative">
      <ReactFlow
        nodes={nodes}
        edges={flowEdges}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        elementsSelectable={false}
      >
        <Background variant={BackgroundVariant.Dots} gap={16} size={1} color="var(--hairline)" />
      </ReactFlow>
      {rootId && (
        <div className="absolute bottom-2 left-2 text-[11px] text-(--text-dim) bg-(--bg-inset) border border-(--hairline) px-2 py-1">
          likely root cause: {byId.get(rootId)?.title ?? rootId}
        </div>
      )}
    </div>
  );
}
