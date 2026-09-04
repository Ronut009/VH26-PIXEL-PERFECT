from datetime import datetime
from uuid import uuid4

from src.contracts import NormalizedEvent
from src.utils.fingerprint import compute_fingerprint


def normalize_prometheus(payload: dict) -> list[NormalizedEvent]:
    events: list[NormalizedEvent] = []

    for alert in payload.get("alerts", []):
        labels = {str(k): str(v) for k, v in alert.get("labels", {}).items()}
        annotations = alert.get("annotations", {})

        service = labels.get("service") or labels.get("job") or "unknown"
        alertname = labels.get("alertname", "unknown")
        severity_raw = labels.get("severity", "info")
        status = "resolved" if alert.get("status") == "resolved" else "firing"
        fingerprint = compute_fingerprint(service, alertname, labels)
        message = annotations.get("summary") or annotations.get("description") or alertname
        fired_at = datetime.fromisoformat(alert["startsAt"].replace("Z", "+00:00"))

        events.append(
            NormalizedEvent(
                event_id=uuid4(),
                fingerprint=fingerprint,
                source="prometheus",
                service=service,
                alertname=alertname,
                severity_raw=severity_raw,
                status=status,
                labels=labels,
                message=message,
                fired_at=fired_at,
                raw_payload=alert,
            )
        )

    return events
