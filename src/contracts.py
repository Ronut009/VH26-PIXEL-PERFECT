from datetime import datetime
from typing import Any, Literal, Optional, TypeAlias
from uuid import UUID

from pydantic import BaseModel, Field


IncidentState: TypeAlias = Literal[
    "OPEN",
    "ACKNOWLEDGED",
    "QUIESCENT",
    "RESOLVED",
]

Severity: TypeAlias = Literal["critical", "high", "medium", "low"]


class CardChange(BaseModel):
    kind: str
    value: str | None = None


class DeliveryIntent(BaseModel):
    channel: Literal["slack", "pagerduty", "email"]
    action: Literal["create", "update", "resolve", "trigger"]
    idempotency_key: str
    payload_json: str


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
    severity_final: Severity
    alert_count: int
    ewma_rate: float
    title: str
    summary: Optional[str] = None


class EngineDecision(BaseModel):
    """The complete transaction-bound engine result consumed by DbWriter."""

    event: NormalizedEvent
    incident_id: UUID
    state: IncidentState
    is_duplicate: bool
    severity_final: Severity
    alert_count: int
    title: str
    summary: Optional[str] = None

    scope_key: str
    stable_fingerprint: str
    quiet_at_ms: int | None = None
    ewma_mean_gap: float = 0.0
    ewma_variance: float = 0.0
    gap_history: list[float] = Field(default_factory=list)

    card_changes: list[CardChange] = Field(default_factory=list)
    is_critical_bypass: bool = False
    bypass_reason: str | None = None
    decision_payload_json: str = "{}"
    delivery_intents: list[DeliveryIntent] = Field(default_factory=list)
    # Flap damping: how many times this incident has closed and come back, and
    # whether that is now a pattern rather than a one-off recurrence.
    reopen_count: int = 0
    is_flapping: bool = False
    root_cause_hint: str | None = None


class GraphUpdate(BaseModel):
    related_incident_ids: list[UUID] = []
    root_cause_hint: Optional[str] = None
