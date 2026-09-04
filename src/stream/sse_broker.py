"""Database-backed server-sent event broker with replay-safe stream identifiers."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import json
from typing import Any, AsyncIterator

import aiosqlite
from fastapi import APIRouter, Request
from fastapi.responses import StreamingResponse

STREAM_ID_STRIDE = 10
_INCIDENT_OFFSET = 1
_GRAPH_OFFSET = 2
_CARD_OFFSET = 3
_METRICS_OFFSET = 4


@dataclass(frozen=True)
class StreamEvent:
    stream_id: int
    event_type: str
    data: dict[str, Any]

    def encode(self) -> str:
        envelope = {"streamId": self.stream_id, **self.data}
        return f"id: {self.stream_id}\nevent: {self.event_type}\ndata: {json.dumps(envelope, separators=(',', ':'))}\n\n"


def _incident_payload(row: aiosqlite.Row) -> dict[str, Any]:
    # The dashboard renders entirely from the stream once it connects, so an
    # incident here has to be as complete as one from GET /v1/incidents/recent.
    # Omitting the routing decision and the timestamps made several panels
    # quietly wrong rather than empty: every incident read as "not delivered",
    # the alert-volume chart had nothing to bucket by, and the EWMA rate showed
    # zero for incidents that were actively firing.
    return {
        "incident": {
            "incidentId": row["incident_id"],
            "title": row["title"],
            "summary": row["summary"],
            "severity": row["severity"],
            "state": row["status"],
            "alertCount": row["alert_count"],
            "quietAtMs": row["quiet_at_ms"],
            "rootCauseHint": row["root_cause_hint"],
            "routeDecision": row["route_decision"],
            "ewmaRate": row["ewma_rate"],
            "firstAlertAt": row["first_alert_at"],
            "lastAlertAt": row["last_alert_at"],
            "createdAt": row["created_at"],
            "updatedAt": row["updated_at"],
        }
    }


async def _latest_stream_id(tx: aiosqlite.Connection) -> int:
    async with tx.execute("SELECT COALESCE(MAX(seq), 0) AS max_seq FROM raw_events") as cursor:
        row = await cursor.fetchone()
    return int(row["max_seq"]) * STREAM_ID_STRIDE


async def read_snapshot(
    tx: aiosqlite.Connection, *, incident_limit: int = 100, edge_limit: int = 200
) -> StreamEvent:
    """Read bounded current state for the initial dashboard render."""

    async with tx.execute(
        """
        SELECT incident_id, title, summary, severity, status, alert_count,
               quiet_at_ms, root_cause_hint, route_decision, ewma_rate,
               first_alert_at, last_alert_at, created_at, updated_at
        FROM incidents
        ORDER BY updated_at DESC
        LIMIT ?
        """,
        (incident_limit,),
    ) as cursor:
        incidents = await cursor.fetchall()
    async with tx.execute(
        """
        SELECT src_incident_id, dst_incident_id, weight, last_seen_at
        FROM edges
        ORDER BY last_seen_at DESC
        LIMIT ?
        """,
        (edge_limit,),
    ) as cursor:
        edges = await cursor.fetchall()

    return StreamEvent(
        stream_id=await _latest_stream_id(tx),
        event_type="snapshot",
        data={
            "incidents": [_incident_payload(row)["incident"] for row in incidents],
            "edges": [
                {
                    "sourceIncidentId": row["src_incident_id"],
                    "targetIncidentId": row["dst_incident_id"],
                    "decayed_joint_weight": row["weight"],
                    "lastSeenAt": row["last_seen_at"],
                }
                for row in edges
            ],
        },
    )


async def _incident_for_event(tx: aiosqlite.Connection, incident_id: str) -> aiosqlite.Row | None:
    async with tx.execute(
        """
        SELECT incident_id, title, summary, severity, status, alert_count,
               quiet_at_ms, root_cause_hint, route_decision, ewma_rate,
               first_alert_at, last_alert_at, created_at, updated_at
        FROM incidents
        WHERE incident_id = ?
        """,
        (incident_id,),
    ) as cursor:
        return await cursor.fetchone()


async def _edges_for_incident(tx: aiosqlite.Connection, incident_id: str) -> list[aiosqlite.Row]:
    async with tx.execute(
        """
        SELECT src_incident_id, dst_incident_id, weight, last_seen_at
        FROM edges
        WHERE src_incident_id = ? OR dst_incident_id = ?
        ORDER BY last_seen_at DESC
        """,
        (incident_id, incident_id),
    ) as cursor:
        return await cursor.fetchall()


async def read_delta_events(
    tx: aiosqlite.Connection, after_stream_id: int, *, limit: int = 100
) -> tuple[StreamEvent, ...]:
    """Read bounded deltas after one replay-safe monotonic SSE cursor."""

    if after_stream_id < 0:
        raise ValueError("after_stream_id must be non-negative")
    raw_sequence = after_stream_id // STREAM_ID_STRIDE
    comparator = ">" if after_stream_id % STREAM_ID_STRIDE == 0 else ">="
    async with tx.execute(
        f"""
        SELECT seq, incident_id, decision_payload_json
        FROM raw_events
        WHERE seq {comparator} ?
        ORDER BY seq ASC
        LIMIT ?
        """,
        (raw_sequence, limit),
    ) as cursor:
        raw_events = await cursor.fetchall()

    deltas: list[StreamEvent] = []
    for raw_event in raw_events:
        base_id = int(raw_event["seq"]) * STREAM_ID_STRIDE
        incident = await _incident_for_event(tx, raw_event["incident_id"])
        if incident is None:
            continue
        candidates = (
            StreamEvent(base_id + _INCIDENT_OFFSET, "incident.upsert", _incident_payload(incident)),
            StreamEvent(
                base_id + _GRAPH_OFFSET,
                "graph.edge.upsert",
                {
                    "edges": [
                        {
                            "sourceIncidentId": edge["src_incident_id"],
                            "targetIncidentId": edge["dst_incident_id"],
                            "decayed_joint_weight": edge["weight"],
                            "lastSeenAt": edge["last_seen_at"],
                        }
                        for edge in await _edges_for_incident(tx, raw_event["incident_id"])
                    ]
                },
            ),
            StreamEvent(
                base_id + _CARD_OFFSET,
                "card.update",
                {
                    "incidentId": raw_event["incident_id"],
                    "decision": json.loads(raw_event["decision_payload_json"]),
                },
            ),
            StreamEvent(
                base_id + _METRICS_OFFSET,
                "metrics.update",
                {
                    "incidentId": incident["incident_id"],
                    "alertCount": incident["alert_count"],
                    "state": incident["status"],
                },
            ),
        )
        deltas.extend(event for event in candidates if event.stream_id > after_stream_id)
    return tuple(deltas)


async def _stream_database(
    database_path: str, request: Request, after_stream_id: int
) -> AsyncIterator[str]:
    connection = await aiosqlite.connect(database_path)
    connection.row_factory = aiosqlite.Row
    try:
        snapshot = await read_snapshot(connection)
        cursor = after_stream_id
        if snapshot.stream_id > cursor:
            yield snapshot.encode()
            cursor = snapshot.stream_id

        while not await request.is_disconnected():
            deltas = await read_delta_events(connection, cursor)
            if deltas:
                for delta in deltas:
                    yield delta.encode()
                    cursor = delta.stream_id
            else:
                yield ": keepalive\n\n"
            await asyncio.sleep(0.25)
    finally:
        await connection.close()


def create_sse_router(database_path: str) -> APIRouter:
    router = APIRouter()

    @router.get("/v1/stream")
    async def stream(request: Request, after: int | None = None) -> StreamingResponse:
        header_cursor = request.headers.get("last-event-id")
        after_stream_id = after if after is not None else int(header_cursor or 0)
        if after_stream_id < 0:
            after_stream_id = 0
        return StreamingResponse(
            _stream_database(database_path, request, after_stream_id),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return router
