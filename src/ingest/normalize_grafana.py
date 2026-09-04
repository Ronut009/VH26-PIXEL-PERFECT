"""Grafana Alerting webhook adapter for PulseGraph's normalized ingest contract."""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from src.contracts import NormalizedEvent
from src.utils.fingerprint import compute_fingerprint


_STATE_TO_STATUS = {
    "alerting": "firing",
    "firing": "firing",
    "ok": "resolved",
    "resolved": "resolved",
}


def _labels(value: object) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise ValueError("alert.labels must be an object")
    labels = {str(key): str(item) for key, item in value.items()}
    if "environment" not in labels and labels.get("env"):
        labels["environment"] = labels["env"]
    return labels


def _parse_starts_at(value: object) -> datetime:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("alert.startsAt must be an ISO-8601 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("alert.startsAt is not a valid ISO-8601 timestamp") from exc
    return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc)


def _message(alert: Mapping[str, Any], alertname: str) -> str:
    value = alert.get("message")
    if isinstance(value, str) and value.strip():
        return value
    annotations = alert.get("annotations")
    if isinstance(annotations, Mapping):
        for key in ("summary", "description", "message"):
            annotation = annotations.get(key)
            if isinstance(annotation, str) and annotation.strip():
                return annotation
    return alertname


def normalize_grafana(payload: dict[str, Any]) -> list[NormalizedEvent]:
    """Normalize every alert in a Grafana Alerting webhook payload."""

    alerts = payload.get("alerts")
    if not isinstance(alerts, list) or not alerts:
        raise ValueError("payload.alerts must be a non-empty list")

    events: list[NormalizedEvent] = []
    for index, alert in enumerate(alerts):
        if not isinstance(alert, Mapping):
            raise ValueError(f"payload.alerts[{index}] must be an object")

        labels = _labels(alert.get("labels"))
        alertname = labels.get("alertname")
        if not alertname:
            raise ValueError(f"payload.alerts[{index}].labels.alertname is required")

        state = alert.get("state", alert.get("status"))
        if not isinstance(state, str):
            raise ValueError(f"payload.alerts[{index}].state must be a string")
        try:
            status = _STATE_TO_STATUS[state.lower()]
        except KeyError as exc:
            raise ValueError(
                f"payload.alerts[{index}].state must be Alerting or OK"
            ) from exc

        source_event_id = alert.get("fingerprint")
        if source_event_id is None or str(source_event_id).strip() == "":
            raise ValueError(f"payload.alerts[{index}].fingerprint is required")

        service = labels.get("service") or labels.get("job") or "unknown"
        raw_payload = dict(payload)
        raw_payload["source_event_id"] = str(source_event_id)

        events.append(
            NormalizedEvent(
                event_id=uuid4(),
                fingerprint=compute_fingerprint(service, alertname, labels),
                source="grafana",
                service=service,
                alertname=alertname,
                severity_raw=labels.get("severity", "info"),
                status=status,  # type: ignore[arg-type]
                labels=labels,
                message=_message(alert, alertname),
                fired_at=_parse_starts_at(alert.get("startsAt")),
                raw_payload=raw_payload,
            )
        )
    return events
