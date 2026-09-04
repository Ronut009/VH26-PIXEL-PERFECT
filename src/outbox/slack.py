import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from src.config import settings
from src.utils.logging import get_logger

logger = get_logger(__name__)

SLACK_API_URL = "https://slack.com/api"

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
}


class SlackRateLimited(Exception):
    def __init__(self, retry_after: float):
        self.retry_after = retry_after
        super().__init__(f"Slack rate limited, retry after {retry_after}s")


def _build_blocks(payload: dict, resolved: bool = False) -> list[dict]:
    severity = payload.get("severity", "medium")
    emoji = "✅" if resolved else _SEVERITY_EMOJI.get(severity, "🟡")
    title = payload.get("title", "Untitled incident")
    summary = payload.get("summary") or "No summary available."
    alert_count = payload.get("alert_count", 1)
    service = payload.get("service", "unknown")
    timestamp = payload.get("timestamp", "")

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"{emoji} {title}", "emoji": True},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary},
        },
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": f"*{alert_count}* alert(s) · *{service}* · {timestamp}",
                }
            ],
        },
    ]

    if resolved:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "✅ *Resolved*"},
            }
        )
    else:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Acknowledge"},
                        "action_id": "acknowledge_incident",
                        "value": payload.get("incident_id", ""),
                    }
                ],
            }
        )

    return blocks


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    response = await client.post(url, headers=_headers(), json=body)

    if response.status_code == 429:
        retry_after = float(response.headers.get("Retry-After", "1"))
        raise SlackRateLimited(retry_after)

    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        raise RuntimeError(f"Slack API error: {data.get('error')}")

    return data


@retry(
    retry=retry_if_exception_type((SlackRateLimited, httpx.TransportError)),
    wait=wait_exponential(multiplier=1, min=1, max=30),
    stop=stop_after_attempt(5),
    reraise=True,
)
async def send(action: str, payload: dict, external_ref: str | None) -> str:
    async with httpx.AsyncClient(timeout=10.0) as client:
        if action == "create":
            body = {
                "channel": settings.SLACK_CHANNEL_ID,
                "blocks": _build_blocks(payload),
                "text": payload.get("title", "New incident"),
            }
            data = await _post(client, f"{SLACK_API_URL}/chat.postMessage", body)
            return data["ts"]

        if action in ("update", "resolve"):
            if not external_ref:
                body = {
                    "channel": settings.SLACK_CHANNEL_ID,
                    "blocks": _build_blocks(payload, resolved=(action == "resolve")),
                    "text": payload.get("title", "Incident update"),
                }
                data = await _post(client, f"{SLACK_API_URL}/chat.postMessage", body)
                return data["ts"]

            body = {
                "channel": settings.SLACK_CHANNEL_ID,
                "ts": external_ref,
                "blocks": _build_blocks(payload, resolved=(action == "resolve")),
                "text": payload.get("title", "Incident update"),
            }
            data = await _post(client, f"{SLACK_API_URL}/chat.update", body)
            return data["ts"]

        raise ValueError(f"Unknown slack action: {action}")
