"""Deterministic protected-emergency bypass classification and artifacts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
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


# Words, with camelCase treated as a boundary: `AuthTokenRefreshFailure`
# becomes auth/token/refresh/failure, and `HTTPError` becomes http/error.
_WORD = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z0-9]*|[a-z0-9]+")

_PAYMENT_SUBJECTS = ("payment", "billing", "checkout")
_PAYMENT_STATES = ("failure", "failed", "declined")

_AUTH_SUBJECTS = ("auth", "authentication", "login", "identity", "sso")
# Deliberately narrower than the payment set: this rule is AUTH_OUTAGE, and an
# outage is what it must fire on.
_AUTH_STATES = ("outage", "unavailable", "unreachable", "down", "offline")

_DATA_SUBJECTS = ("data", "database", "storage", "backup", "replication")
_DATA_STATES = ("loss", "corrupt", "corrupted", "deleted", "destroyed")


def _event_tokens(event: NormalizedEvent) -> frozenset[str]:
    """Split everything the event says into whole words.

    This used to be a substring search over the joined text, which silently
    over-fires: `down` matches `downstream`, `auth` matches `oauth`, and `data`
    matches `metadata` - so an alert carrying a `metadata` label was one
    `deleted` away from paging as DATA_LOSS. Requiring word boundaries in the
    joined string is not enough on its own, because alert names are camelCase
    far more often than not; the words have to be split out first.

    Trailing plurals collapse (`failures` -> `failure`) so a rule does not need
    to enumerate both.
    """

    parts = [event.service, event.alertname, event.message]
    parts.extend(f"{key} {value}" for key, value in event.labels.items())

    tokens: set[str] = set()
    for word in _WORD.findall(" ".join(part for part in parts if part)):
        word = word.lower()
        tokens.add(word)
        if len(word) > 3 and word.endswith("s"):
            tokens.add(word[:-1])
    return frozenset(tokens)


def _has_any(tokens: frozenset[str], candidates: tuple[str, ...]) -> bool:
    return not tokens.isdisjoint(candidates)


def classify_protected_critical(event: NormalizedEvent) -> CriticalBypassDecision:
    """Identify emergencies that must never enter dedupe or EWMA filtering.

    The keyword rules below are a safety net for an emergency someone forgot to
    label, not the main path - `severity: critical` and `priority: P0` are.
    That matters for how wide they should be: a bypassed alert skips
    consolidation entirely and pages per alert, so a rule that fires on routine
    traffic manufactures exactly the alert fatigue this system exists to
    remove. When in doubt these rules stay narrow, because the consolidated
    path still delivers the alert - it just does not page for each one.
    """

    if event.severity_raw.lower() == "critical":
        return CriticalBypassDecision(True, "SEVERITY_CRITICAL")
    if event.labels.get("priority", "").upper() == "P0":
        return CriticalBypassDecision(True, "PRIORITY_P0")

    tokens = _event_tokens(event)
    if _has_any(tokens, _PAYMENT_SUBJECTS) and _has_any(tokens, _PAYMENT_STATES):
        return CriticalBypassDecision(True, "PAYMENT_FAILURE")
    if _has_any(tokens, _AUTH_SUBJECTS) and _has_any(tokens, _AUTH_STATES):
        return CriticalBypassDecision(True, "AUTH_OUTAGE")
    if _has_any(tokens, _DATA_SUBJECTS) and _has_any(tokens, _DATA_STATES):
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
