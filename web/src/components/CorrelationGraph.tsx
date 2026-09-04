"use client";

import { useMemo } from "react";
import { ReactFlow, Background, BackgroundVariant, type Node, type Edge } from "@xyflow/react";
import "@xyflow/react/dist/style.css";
import type { Incident, IncidentEdge } from "@/lib/types";

const COL_W = 260;
const ROW_H = 84;

/**
 * Causal layering: incidents nothing points at sit on the top row, whatever
 * they caused sits below. This is what makes the graph answer "which one do I
 * fix" rather than only "these are related".
 */
export function layerNodes(ids: string[], edges: IncidentEdge[]): Map<string, number> {
  const inbound = new Map<string, string[]>();
  ids.forEach((id) => inbound.set(id, []));
  edges.forEach((edge) => inbound.get(edge.dst_incident_id)?.push(edge.src_incident_id));

  const depth = new Map<string, number>();
  const resolve = (id: string, seen: Set<string>): number => {
    if (depth.has(id)) return depth.get(id) as number;
    if (seen.has(id)) return 0;
    seen.add(id);
    const parents = inbound.get(id) ?? [];
    const value = parents.length === 0 ? 0 : Math.max(...parents.map((p) => resolve(p, seen) + 1));
    depth.set(id, value);
    return value;
  };
  ids.forEach((id) => resolve(id, new Set()));
  return depth;
}

export function CorrelationGraph({
  incidents,
  edges,
  onSelect,
  selectedId,
}: {
  incidents: Incident[];
  edges: IncidentEdge[];
  onSelect: (incident: Incident) => void;
  selectedId: string | null;
}) {
  const byId = useMemo(() => new Map(incidents.map((i) => [i.incident_id, i])), [incidents]);

  const { nodes, flowEdges } = useMemo(() => {
    // When two incidents keep firing together the engine records a directed
    // edge each way, so the raw set is cyclic: every node has a parent, and
    // "cause above effect" cannot be layered from it. Keep the heavier
    // direction of each pair -- the same outbound joint weight the backend's
    // root-cause ranker scores on -- which yields an acyclic graph whose
    // vertical order actually means something.
    const dominant = new Map<string, IncidentEdge>();
    for (const edge of edges) {
      const pair = [edge.src_incident_id, edge.dst_incident_id].sort().join("|");
      const held = dominant.get(pair);
      if (!held || edge.weight > held.weight) dominant.set(pair, edge);
    }
    const directed = Array.from(dominant.values());

    const ids = Array.from(
      new Set(directed.flatMap((e) => [e.src_incident_id, e.dst_incident_id])),
    ).filter((id) => byId.has(id));

    const depth = layerNodes(ids, directed);
    const perRow = new Map<number, number>();

    const nodes: Node[] = ids.map((id) => {
      const incident = byId.get(id) as Incident;
      const row = depth.get(id) ?? 0;
      const col = perRow.get(row) ?? 0;
      perRow.set(row, col + 1);
      const isCritical = incident.severity === "critical";
      const isSelected = id === selectedId;

      return {
        id,
        position: { x: col * COL_W, y: row * ROW_H },
        data: { label: incident.title },
        style: {
          background: isSelected ? "#EFF4FF" : "#FFFFFF",
          border: isCritical ? "1px solid #EF4444" : "1px solid #E5EAF0",
          borderRadius: 4,
          color: "#172033",
          fontSize: 12,
          lineHeight: 1.4,
          padding: "10px 12px",
          width: COL_W - 40,
          boxShadow: "none",
        },
      };
    });

    const heaviest = Math.max(1, ...directed.map((e) => e.weight));
    const flowEdges: Edge[] = directed
      .filter((e) => byId.has(e.src_incident_id) && byId.has(e.dst_incident_id))
      .map((e) => ({
        id: `${e.src_incident_id}->${e.dst_incident_id}`,
        source: e.src_incident_id,
        target: e.dst_incident_id,
        animated: false,
        label: e.weight.toFixed(1),
        labelShowBg: true,
        labelBgPadding: [4, 2] as [number, number],
        labelBgStyle: { fill: "#FFFFFF", stroke: "#E5EAF0" },
        labelStyle: { fill: "#64748B", fontSize: 11 },
        // Thicker means the two fired together more often, so the strongest
        // link is visible without reading the numbers.
        style: { stroke: "#94A3B8", strokeWidth: 1 + (e.weight / heaviest) * 2 },
      }));

    return { nodes, flowEdges };
  }, [edges, byId, selectedId]);

  if (nodes.length === 0) {
    return (
      <div className="flex h-full w-full items-center justify-center px-6 text-center">
        <div>
          <p className="text-[13px] font-medium text-text">No correlations recorded</p>
          <p className="mt-2 text-[12px] text-text-2">
            Edges appear when two incidents repeatedly fire inside the same window.
          </p>
        </div>
      </div>
    );
  }

  return (
    <div className="h-full w-full">
      <ReactFlow
        nodes={nodes}
        edges={flowEdges}
        fitView
        proOptions={{ hideAttribution: true }}
        nodesDraggable={false}
        nodesConnectable={false}
        onNodeClick={(_, node) => {
          const incident = byId.get(node.id);
          if (incident) onSelect(incident);
        }}
      >
        <Background variant={BackgroundVariant.Dots} gap={20} size={1} color="#E5EAF0" />
      </ReactFlow>
    </div>
  );
}
