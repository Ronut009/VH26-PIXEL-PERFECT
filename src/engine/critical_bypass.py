"""Deterministic protected-emergency bypass classification and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import TYPE_CHECKING
from uuid import UUID

if TYPE_CHECKING:
    from src.contracts import NormalizedEvent


@dataclass(frozen=True)
class CriticalBypassDecision:
    should_bypass: bool
    reason: str | None = None


@dataclass(frozen=True)
class DeliveryIntent:
    """A durable-outbox row payload that DbWriter must persist atomically."""

    incident_id: UUID
    provider: str
    action: str
    idempotency_key: str
    payload_json: str


@dataclass(frozen=True)
class AuditLedgerEntry:
    """Immutable decision payload for inclusion in Yash's hash-chain append."""

    event_id: UUID
    incident_id: UUID
    decision: str
    reason: str
    occurred_at_ms: int
    canonical_payload_json: str


def _event_text(event: NormalizedEvent) -> str:
    parts = [event.service, event.alertname, event.message]
    parts.extend(f"{key}={value}" for key, value in event.labels.items())
    return " ".join(parts).lower()


def _has_any(text: str, tokens: tuple[str, ...]) -> bool:
    return any(token in text for token in tokens)


def classify_protected_critical(event: NormalizedEvent) -> CriticalBypassDecision:
    """Identify emergencies that must never enter dedupe or EWMA filtering."""

    if event.severity_raw.lower() == "critical":
        return CriticalBypassDecision(True, "SEVERITY_CRITICAL")
    if event.labels.get("priority", "").upper() == "P0":
        return CriticalBypassDecision(True, "PRIORITY_P0")

    text = _event_text(event)
    if _has_any(text, ("payment", "billing", "checkout")) and _has_any(
        text, ("failure", "failed", "declined", "error")
    ):
        return CriticalBypassDecision(True, "PAYMENT_FAILURE")
    if _has_any(text, ("auth", "authentication", "login", "identity")) and _has_any(
        text, ("outage", "unavailable", "failure", "failed", "down")
    ):
        return CriticalBypassDecision(True, "AUTH_OUTAGE")
    if _has_any(text, ("data", "database", "storage", "backup", "replication")) and _has_any(
        text, ("loss", "corrupt", "deleted", "destroyed")
    ):
        return CriticalBypassDecision(True, "DATA_LOSS")

    return CriticalBypassDecision(False)


def _event_time_ms(event: NormalizedEvent) -> int:
    fired_at: datetime = event.fired_at
    if fired_at.tzinfo is None:
        fired_at = fired_at.replace(tzinfo=timezone.utc)
    return int(fired_at.timestamp() * 1000)


def build_bypass_artifacts(
    event: NormalizedEvent,
    incident_id: UUID,
    decision: CriticalBypassDecision,
) -> tuple[tuple[DeliveryIntent, ...], AuditLedgerEntry]:
    """Build immediate outbox and audit data; caller owns persistence."""

    if not decision.should_bypass or decision.reason is None:
        raise ValueError("cannot build bypass artifacts for a non-bypassed event")

    base_payload = {
        "incident_id": str(incident_id),
        "event_id": str(event.event_id),
        "service": event.service,
        "alertname": event.alertname,
        "message": event.message,
        "severity": "critical",
        "bypass_reason": decision.reason,
    }
    payload_json = json.dumps(base_payload, sort_keys=True, separators=(",", ":"))
    key_prefix = f"critical-bypass:{event.event_id}:{incident_id}"
    intents = (
        DeliveryIntent(
            incident_id=incident_id,
            provider="pagerduty",
            action="trigger",
            idempotency_key=f"{key_prefix}:pagerduty",
            payload_json=payload_json,
        ),
        DeliveryIntent(
            incident_id=incident_id,
            provider="slack",
            action="post_card",
            idempotency_key=f"{key_prefix}:slack",
            payload_json=payload_json,
        ),
    )
    audit_payload = {
        "event_id": str(event.event_id),
        "incident_id": str(incident_id),
        "decision": "CRITICAL_BYPASS",
        "reason": decision.reason,
    }
    audit_entry = AuditLedgerEntry(
        event_id=event.event_id,
        incident_id=incident_id,
        decision="CRITICAL_BYPASS",
        reason=decision.reason,
        occurred_at_ms=_event_time_ms(event),
        canonical_payload_json=json.dumps(
            audit_payload, sort_keys=True, separators=(",", ":")
        ),
    )
    return intents, audit_entry


__all__ = [
    "AuditLedgerEntry",
    "CriticalBypassDecision",
    "DeliveryIntent",
    "build_bypass_artifacts",
    "classify_protected_critical",
]
