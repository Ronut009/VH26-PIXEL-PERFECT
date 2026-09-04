"""Datadog webhook adapter for PulseGraph's normalized ingest contract."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.contracts import NormalizedEvent
from src.utils.fingerprint import compute_fingerprint


_TRANSITION_TO_STATUS = {
    "triggered": "firing",
    "recovered": "resolved",
}
_PRIORITY_TO_SEVERITY = {
    "p1": "critical",
    "p2": "high",
    "p3": "medium",
    "p4": "low",
}


def _required_text(payload: Mapping[str, Any], key: str, context: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{context}.{key} must be a non-empty string")
    return value


def _parse_timestamp(value: object) -> datetime:
    """Accept Datadog's epoch seconds as well as ISO-8601 timestamps."""

    if isinstance(value, bool) or value is None:
        raise ValueError("event.timestamp must be an epoch timestamp or ISO-8601 string")

    if isinstance(value, (int, float)):
        numeric = float(value)
    elif isinstance(value, str):
        try:
            numeric = float(value)
        except ValueError:
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError as exc:
                raise ValueError("event.timestamp is not a valid timestamp") from exc
            return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)
    else:
        raise ValueError("event.timestamp must be an epoch timestamp or ISO-8601 string")

    # Datadog timestamps are normally seconds; accept milliseconds defensively.
    if abs(numeric) >= 100_000_000_000:
        numeric /= 1000
    try:
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as exc:
        raise ValueError("event.timestamp is not a valid epoch timestamp") from exc


def _tags_to_labels(tags: object) -> dict[str, str]:
    if not isinstance(tags, list) or not all(isinstance(tag, str) for tag in tags):
        raise ValueError("event.tags must be a list of strings")

    labels: dict[str, str] = {}
    for tag in tags:
        key, separator, value = tag.partition(":")
        key = key.strip()
        if not key:
            continue
        labels[key] = value.strip() if separator else ""

    # Datadog conventionally calls this tag `env`, but the engine's scope key
    # contract uses `environment`.
    if "environment" not in labels and labels.get("env"):
        labels["environment"] = labels["env"]
    return labels


def normalize_datadog(payload: dict[str, Any]) -> list[NormalizedEvent]:
    """Normalize one real Datadog event-webhook payload into one event."""

    event = payload.get("event")
    if not isinstance(event, Mapping):
        raise ValueError("payload.event must be an object")

    title = _required_text(event, "title", "event")
    message = _required_text(event, "text", "event")
    transition = _required_text(event, "alert_transition", "event").lower()
    try:
        status = _TRANSITION_TO_STATUS[transition]
    except KeyError as exc:
        raise ValueError("event.alert_transition must be Triggered or Recovered") from exc

    priority = _required_text(event, "priority", "event")
    severity_raw = _PRIORITY_TO_SEVERITY.get(priority.lower(), priority.lower())
    labels = _tags_to_labels(event.get("tags"))
    source_event_id = event.get("id")
    if source_event_id is None or str(source_event_id).strip() == "":
        raise ValueError("event.id is required")

    service = labels.get("service") or labels.get("service_name") or "unknown"
    raw_payload = dict(payload)
    # NormalizedEvent has no source-event-id field; retain the provider ID in
    # its audit payload without adding it to fingerprinting labels.
    raw_payload["source_event_id"] = str(source_event_id)

    return [
        NormalizedEvent(
            event_id=uuid4(),
            fingerprint=compute_fingerprint(service, title, labels),
            source="datadog",
            service=service,
            alertname=title,
            severity_raw=severity_raw,
            status=status,  # type: ignore[arg-type]
            labels=labels,
            message=message,
            fired_at=_parse_timestamp(event.get("timestamp")),
            raw_payload=raw_payload,
        )
    ]
