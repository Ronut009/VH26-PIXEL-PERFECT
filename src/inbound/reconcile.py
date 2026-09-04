"""Apply an externally-originated state change to an incident.

Until now state only ever flowed outward: PulseGraph told Slack and PagerDuty
what happened and never learned anything back. That produced the failure this
module exists to fix - an engineer acknowledges a page in PagerDuty, fixes the
problem, and when Slack recovers the system cheerfully posts a fresh actionable
card for an incident that was handled twenty minutes ago.

Two design choices matter here:

*External actions go through the same path as alerts.* A human clicking
Acknowledge produces a synthetic ``NormalizedEvent`` and a real
``EngineDecision``, persisted by the same ``persist_decision`` used by ingest.
So a human action lands in the hash-chained audit ledger exactly like a machine
one, and it emits its own outbox intent - which is why acknowledging in
PagerDuty updates the Slack card, and vice versa.

*Every callback is idempotent.* Providers retry deliveries and users
double-click. The provider's own delivery id is the primary key of
``inbound_events``, so replaying a callback is recorded and discarded rather
than re-transitioning the incident or appending a second ledger entry.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any
from uuid import UUID, uuid4

import aiosqlite

from src.contracts import CardChange, DeliveryIntent, EngineDecision, NormalizedEvent
from src.engine.db_adapter import persist_decision
from src.engine.incident_machine import transition_state
from src.graph.storm_grouping import (
    redirect_member_deliveries,
    refresh_group_for_member,
)

ACKNOWLEDGE = "acknowledge"
RESOLVE = "resolve"

_TRIGGER_FOR_KIND = {ACKNOWLEDGE: "ACKNOWLEDGE", RESOLVE: "RESOLVE"}


@dataclass(frozen=True)
class ExternalAction:
    """One signed, provider-originated request to change an incident."""

    inbound_id: str
    provider: str          # slack | pagerduty | system
    kind: str              # acknowledge | resolve
    incident_id: str
    actor: str | None = None
    detail: str | None = None
    payload_sha256: str = ""
    # How much this resolution is worth trusting. "operator" means a human
    # asserted it; "inferred_silence" means nobody did and we concluded it from
    # the absence of alerts. The distinction has to survive into the record,
    # because they are very different claims.
    resolution_source: str = "operator"


@dataclass(frozen=True)
class ReconcileResult:
    status: str            # applied | duplicate | ignored | rejected
    incident_id: str | None = None
    from_state: str | None = None
    to_state: str | None = None
    detail: str | None = None

    @property
    def changed(self) -> bool:
        return self.status == "applied"


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def _now() -> str:
    return _iso(datetime.now(timezone.utc))


def _as_json_dict(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _parse(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


async def already_seen(tx: aiosqlite.Connection, inbound_id: str) -> bool:
    async with tx.execute(
        "SELECT 1 FROM inbound_events WHERE inbound_id = ?", (inbound_id,)
    ) as cursor:
        return await cursor.fetchone() is not None


async def record_inbound(
    tx: aiosqlite.Connection, action: ExternalAction, status: str, detail: str | None
) -> None:
    await tx.execute(
        """
        INSERT INTO inbound_events (
            inbound_id, provider, kind, incident_id, actor, status, detail,
            payload_sha256, received_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(inbound_id) DO NOTHING
        """,
        (
            action.inbound_id,
            action.provider,
            action.kind,
            action.incident_id,
            action.actor,
            status,
            detail,
            action.payload_sha256,
            _now(),
        ),
    )


async def resolve_incident_id_from_ref(
    tx: aiosqlite.Connection, channel: str, external_ref: str
) -> str | None:
    """Map a provider handle back to our incident.

    PagerDuty identifies an incident by the ``dedup_key`` we gave it, and Slack
    by the message ``ts``. Both are stored on the outbox row that created them,
    so the outbox doubles as the outbound identity map.
    """

    async with tx.execute(
        """
        SELECT incident_id FROM outbox
        WHERE channel = ? AND external_ref = ?
        ORDER BY outbox_id DESC LIMIT 1
        """,
        (channel, external_ref),
    ) as cursor:
        row = await cursor.fetchone()
    return row["incident_id"] if row else None


async def _load_incident(tx: aiosqlite.Connection, incident_id: str) -> dict | None:
    async with tx.execute(
        """
        SELECT
            i.incident_id, i.scope_key, i.stable_fingerprint, i.title, i.summary,
            i.severity, i.status, i.alert_count, i.last_alert_at,
            i.ewma_mean_gap, i.ewma_variance, i.gap_history_json, i.root_cause_hint,
            r.service, r.alertname, r.severity_raw, r.labels_json
        FROM incidents AS i
        LEFT JOIN raw_events AS r
          ON r.event_id = (
              SELECT latest.event_id FROM raw_events AS latest
              WHERE latest.incident_id = i.incident_id
              ORDER BY latest.seq DESC LIMIT 1
          )
        WHERE i.incident_id = ?
        """,
        (incident_id,),
    ) as cursor:
        row = await cursor.fetchone()
    return dict(row) if row else None


def _build_decision(
    row: dict, action: ExternalAction, next_state: str
) -> EngineDecision:
    """Turn a human action into a first-class engine decision."""

    labels = _as_json_dict(row["labels_json"])
    event_id = uuid4()
    incident_id = row["incident_id"]
    verb = "acknowledged" if action.kind == ACKNOWLEDGE else "resolved"

    event = NormalizedEvent(
        event_id=event_id,
        fingerprint=f"external:{action.provider}:{action.inbound_id}",
        source="generic",
        service=row["service"] or "pulsegraph-engine",
        alertname=row["alertname"] or "ExternalAction",
        severity_raw=row["severity_raw"] or row["severity"],
        # An acknowledgement is not a new firing signal, and a resolve is the
        # end of one. Reporting the true status keeps the ledger honest.
        status="resolved" if action.kind == RESOLVE else "firing",
        labels={str(key): str(value) for key, value in labels.items()},
        message=(
            action.detail
            if action.provider == "system"
            else (
                f"Incident {verb} in {action.provider}"
                + (f" by {action.actor}" if action.actor else "")
            )
        ),
        fired_at=_parse(row["last_alert_at"]),
        raw_payload={
            "kind": "EXTERNAL_ACTION",
            "provider": action.provider,
            "action": action.kind,
            "actor": action.actor,
            "inbound_id": action.inbound_id,
            "incident_id": incident_id,
        },
    )

    try:
        gap_history = [float(value) for value in json.loads(row["gap_history_json"])]
    except (TypeError, ValueError, json.JSONDecodeError):
        gap_history = []

    card_fields = {
        "incident_id": incident_id,
        "state": next_state,
        "acknowledged_by": action.actor,
        "via": action.provider,
    }
    # Mark an inferred resolution on the card itself. A responder must be able
    # to tell "a human confirmed this is fixed" from "the alerts stopped, so we
    # assume it is" without opening the dashboard.
    if action.resolution_source != "operator":
        card_fields["resolution_source"] = action.resolution_source

    card_payload = json.dumps(card_fields, sort_keys=True, separators=(",", ":"))

    # Notify every channel except the one the human just acted in - they can
    # already see the result of their own click.
    intents = [
        DeliveryIntent(
            channel=channel,  # type: ignore[arg-type]
            action="resolve" if action.kind == RESOLVE else "update",
            idempotency_key=f"external:{action.inbound_id}:{channel}",
            payload_json=card_payload,
        )
        for channel in ("slack", "pagerduty")
        if channel != action.provider
    ]

    return EngineDecision(
        event=event,
        incident_id=UUID(incident_id),
        state=next_state,  # type: ignore[arg-type]
        is_duplicate=True,
        severity_final=row["severity"],
        alert_count=int(row["alert_count"]),
        title=row["title"],
        summary=row["summary"],
        scope_key=row["scope_key"],
        stable_fingerprint=row["stable_fingerprint"],
        # An acknowledged or resolved incident is no longer waiting for quiet.
        quiet_at_ms=None,
        ewma_mean_gap=float(row["ewma_mean_gap"]),
        ewma_variance=float(row["ewma_variance"]),
        gap_history=gap_history,
        card_changes=[CardChange(kind=f"STATE_{row['status']}_TO_{next_state}")],
        decision_payload_json=json.dumps(
            {
                "event_id": str(event_id),
                "incident_id": incident_id,
                "external_action": action.kind,
                "provider": action.provider,
                "actor": action.actor,
                "state": next_state,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        delivery_intents=intents,
        root_cause_hint=row["root_cause_hint"],
    )


async def apply_external_action(
    tx: aiosqlite.Connection, action: ExternalAction
) -> ReconcileResult:
    """Apply one signed provider callback. The caller owns the transaction."""

    if await already_seen(tx, action.inbound_id):
        return ReconcileResult("duplicate", action.incident_id, detail="replayed")

    trigger = _TRIGGER_FOR_KIND.get(action.kind)
    if trigger is None:
        await record_inbound(tx, action, "ignored", f"unknown kind: {action.kind}")
        return ReconcileResult("ignored", action.incident_id, detail="unknown kind")

    row = await _load_incident(tx, action.incident_id)
    if row is None:
        await record_inbound(tx, action, "ignored", "unknown incident")
        return ReconcileResult("ignored", action.incident_id, detail="unknown incident")

    current_state = row["status"]
    next_state = transition_state(current_state, trigger)

    if next_state is None:
        # Not an error. Acknowledging an already-acknowledged incident, or
        # resolving one that is already resolved, is what a second responder
        # naturally does; it is recorded and it changes nothing.
        await record_inbound(
            tx, action, "ignored", f"no transition from {current_state}"
        )
        return ReconcileResult(
            "ignored", action.incident_id, current_state, current_state, "no-op"
        )

    decision = _build_decision(row, action, next_state)
    await persist_decision(tx, decision)

    now = _now()
    if action.kind == ACKNOWLEDGE:
        await tx.execute(
            """
            UPDATE incidents
            SET acknowledged_at = COALESCE(acknowledged_at, ?),
                acknowledged_by = COALESCE(acknowledged_by, ?),
                acknowledged_via = COALESCE(acknowledged_via, ?)
            WHERE incident_id = ?
            """,
            (now, action.actor, action.provider, action.incident_id),
        )
    else:
        await tx.execute(
            """
            UPDATE incidents
            SET resolved_at = COALESCE(resolved_at, ?),
                resolved_via = COALESCE(resolved_via, ?),
                resolution_source = COALESCE(resolution_source, ?),
                resolution_detail = COALESCE(resolution_detail, ?)
            WHERE incident_id = ?
            """,
            (
                now,
                action.provider,
                action.resolution_source,
                action.detail,
                action.incident_id,
            ),
        )

    # A member resolving changes the storm it belongs to: its severity, its
    # member count, and whether the whole cascade is now over.
    assignment = await refresh_group_for_member(tx, action.incident_id)
    if (
        assignment is not None
        and assignment.is_multi_member
        and assignment.anchor_incident_id != action.incident_id
    ):
        await redirect_member_deliveries(
            tx, action.incident_id, assignment.anchor_incident_id
        )

    await record_inbound(tx, action, "applied", f"{current_state} -> {next_state}")
    return ReconcileResult(
        "applied", action.incident_id, current_state, next_state, None
    )


__all__ = [
    "ACKNOWLEDGE",
    "RESOLVE",
    "ExternalAction",
    "ReconcileResult",
    "apply_external_action",
    "already_seen",
    "record_inbound",
    "resolve_incident_id_from_ref",
]
