from uuid import uuid4

from src.contracts import GraphUpdate, IncidentDecision, NormalizedEvent

_SEVERITY_MAP = {
    "critical": "critical",
    "error": "high",
    "high": "high",
    "warning": "medium",
    "medium": "medium",
    "info": "low",
    "low": "low",
}


def _map_severity(severity_raw: str) -> str:
    return _SEVERITY_MAP.get(severity_raw.lower(), "medium")


async def stub_process_incident(db_conn, event: NormalizedEvent) -> IncidentDecision:
    return IncidentDecision(
        incident_id=uuid4(),
        status="new",
        is_duplicate=False,
        severity_final=_map_severity(event.severity_raw),
        alert_count=1,
        ewma_rate=0.0,
        title=f"{event.service} — {event.alertname}",
        summary=None,
    )


async def stub_update_graph(db_conn, event: NormalizedEvent, decision: IncidentDecision) -> GraphUpdate:
    return GraphUpdate(related_incident_ids=[], root_cause_hint=None)
