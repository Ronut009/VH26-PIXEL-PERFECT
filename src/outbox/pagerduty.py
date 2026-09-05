import httpx

from src.config import settings
from src.outbox.failure_policy import RateLimited
from src.utils.logging import get_logger

logger = get_logger(__name__)

PD_EVENTS_URL = "https://events.pagerduty.com/v2/enqueue"

_SEVERITY_MAP = {
    "critical": "critical",
    "high": "error",
    "medium": "warning",
    "low": "info",
}

_ACTION_MAP = {
    "create": "trigger",
    "update": "acknowledge",
    "resolve": "resolve",
}


def _build_event_payload(payload: dict) -> dict:
    summary = payload.get("title", "Incident")
    # A failover page has to be self-explanatory: the responder is being woken
    # by PagerDuty for something that would normally have been a Slack card.
    if payload.get("failover_from"):
        summary = f"[{payload['failover_from']} unreachable] {summary}"

    return {
        "summary": summary,
        "source": payload.get("service", "unknown"),
        "severity": _SEVERITY_MAP.get(payload.get("severity", "critical"), "critical"),
        "timestamp": payload.get("timestamp"),
        "custom_details": {
            "summary": payload.get("summary"),
            "alert_count": payload.get("alert_count"),
            "root_cause_hint": payload.get("root_cause_hint"),
            "state": payload.get("state"),
            "failover_from": payload.get("failover_from"),
            "dashboard_incident_id": payload.get("incident_id"),
        },
    }


async def probe() -> None:
    """Liveness check for the Events API that enqueues nothing.

    A deliberately invalid body gets a 400 from a healthy PagerDuty, which is
    all the breaker needs: the endpoint answered. Only a transport failure or a
    5xx means the channel is actually down. ``raise_for_status`` is not used
    here for that exact reason.
    """

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(PD_EVENTS_URL, json={})
        if response.status_code >= 500:
            response.raise_for_status()


async def send(action: str, payload: dict, external_ref: str | None) -> str:
    # PagerDuty's own dedup_key is the incident id, so a failover trigger for an
    # incident that critical-bypass already paged collapses into the existing
    # PagerDuty incident instead of paging a second time.
    dedup_key = external_ref or payload.get("incident_id")
    event_action = _ACTION_MAP.get(action)
    if event_action is None:
        raise ValueError(f"Unknown pagerduty action: {action}")

    body = {
        "routing_key": settings.PAGERDUTY_INTEGRATION_KEY,
        "event_action": event_action,
        "dedup_key": dedup_key,
    }

    if event_action == "trigger":
        body["payload"] = _build_event_payload(payload)

    async with httpx.AsyncClient(timeout=10.0) as client:
        response = await client.post(PD_EVENTS_URL, json=body)

        if response.status_code == 429:
            raise RateLimited(float(response.headers.get("Retry-After", "1")))

        response.raise_for_status()
        data = response.json()

        if data.get("status") not in ("success",):
            raise RuntimeError(f"PagerDuty API error: {data}")

        return data.get("dedup_key", dedup_key)
