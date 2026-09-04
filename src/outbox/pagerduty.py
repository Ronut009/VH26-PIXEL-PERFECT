import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import settings
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


class PagerDutyRateLimited(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"PagerDuty rate limited, retry after {retry_after}s")


def _build_event_payload(payload: dict) -> dict:
    return {
        "summary": payload.get("title", "Incident"),
        "source": payload.get("service", "unknown"),
        "severity": _SEVERITY_MAP.get(payload.get("severity", "critical"), "critical"),
        "timestamp": payload.get("timestamp"),
        "custom_details": {
            "summary": payload.get("summary"),
            "alert_count": payload.get("alert_count"),
            "root_cause_hint": payload.get("root_cause_hint"),
        },
    }


@retry(
    retry=retry_if_exception_type((PagerDutyRateLimited, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def send(action: str, payload: dict, external_ref: str | None) -> str:
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
            retry_after = float(response.headers.get("Retry-After", "1"))
            raise PagerDutyRateLimited(retry_after)

        response.raise_for_status()
        data = response.json()

        if data.get("status") not in ("success",):
            raise RuntimeError(f"PagerDuty API error: {data}")

        return data.get("dedup_key", dedup_key)
