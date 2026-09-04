from datetime import datetime
from typing import Literal, Optional, TypeAlias
from uuid import UUID

from pydantic import BaseModel


IncidentState: TypeAlias = Literal[
    "OPEN",
    "ACKNOWLEDGED",
    "QUIESCENT",
    "RESOLVED",
]


class NormalizedEvent(BaseModel):
    event_id: UUID
    fingerprint: str
    source: Literal["prometheus", "datadog", "grafana", "generic"]
    service: str
    alertname: str
    severity_raw: str
    status: Literal["firing", "resolved"]
    labels: dict[str, str]
    message: str
    fired_at: datetime
    raw_payload: dict


class IncidentDecision(BaseModel):
    incident_id: UUID
    status: IncidentState
    is_duplicate: bool
    severity_final: Literal["critical", "high", "medium", "low"]
    alert_count: int
    ewma_rate: float
    title: str
    summary: Optional[str] = None


class GraphUpdate(BaseModel):
    related_incident_ids: list[UUID] = []
    root_cause_hint: Optional[str] = None
