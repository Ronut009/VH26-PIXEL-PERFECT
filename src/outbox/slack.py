import httpx

from src.config import settings
from src.outbox.failure_policy import (
    ChannelUnavailable,
    FailureKind,
    MessageRejected,
    RateLimited,
    classify_slack_error,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

SLACK_API_URL = "https://slack.com/api"

_SEVERITY_EMOJI = {
    "critical": "🔴",
    "high": "🟠",
    "medium": "🟡",
    "low": "🔵",
}


def _build_storm_blocks(payload: dict, resolved: bool) -> list[dict]:
    """One card for a whole cascade, cause first.

    The point is not that it is shorter. It is that a responder reads the
    causal chain in one place instead of reconstructing it from N cards that
    each say "something is wrong" and separately name the same root cause.
    """

    group = payload["group"]
    members = group.get("members", [])
    root_id = group.get("root_incident_id")
    emoji = "✅" if resolved else _SEVERITY_EMOJI.get(group.get("severity"), "🟡")

    root = next((m for m in members if m["incident_id"] == root_id), None)
    consequences = [m for m in members if m["incident_id"] != root_id]

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{emoji} Correlated storm — {group['member_count']} services",
                "emoji": True,
            },
        }
    ]

    if root is not None:
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        "*Likely cause*\n"
                        f":arrow_right: *{root['title']}* — "
                        f"{root['alert_count']} alert(s)"
                    ),
                },
            }
        )

    if consequences:
        # Capped, because a 40-service cascade would otherwise produce a card
        # too long for anyone to read - which is the problem, not the fix.
        shown = consequences[:8]
        lines = [
            f"• *{member['title']}* — {member['alert_count']} alert(s) · {member['status']}"
            for member in shown
        ]
        if len(consequences) > len(shown):
            lines.append(f"• _…and {len(consequences) - len(shown)} more_")
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": "*Downstream effects*\n" + "\n".join(lines),
                },
            }
        )

    blocks.append(
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"*{group['total_alert_count']}* alerts across "
                        f"*{group['member_count']}* incidents · one page instead of "
                        f"{group['member_count']} · since {group.get('started_at', '')}"
                    ),
                }
            ],
        }
    )

    if resolved:
        blocks.append(
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": "✅ *Storm resolved* — all members closed"},
            }
        )
    else:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Acknowledge storm"},
                        "action_id": "acknowledge_incident",
                        "value": group.get("anchor_incident_id", ""),
                    }
                ],
            }
        )

    return blocks


def _build_merged_blocks(payload: dict) -> list[dict]:
    """Final edit of a card whose incident turned out to be part of a storm."""

    return [
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": (
                    "🔗 *Merged into a correlated storm* — this incident is a "
                    "symptom of a larger event and is now tracked on the storm "
                    "card for incident "
                    f"`{payload.get('merged_into', 'unknown')}`. "
                    "No separate action needed here."
                ),
            },
        }
    ]


def _build_blocks(payload: dict, resolved: bool = False) -> list[dict]:
    if payload.get("merged_into"):
        return _build_merged_blocks(payload)
    if payload.get("group"):
        return _build_storm_blocks(payload, resolved)

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

    if payload.get("flapping"):
        blocks.append(
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"🔁 *Flapping* — resolved and reopened "
                        f"*{payload.get('reopen_count', 0)}* times. Updates are "
                        "throttled while this continues. Repeated cycling "
                        "usually means the alert threshold is wrong rather "
                        "than the service being unhealthy."
                    ),
                },
            }
        )

    root_cause = payload.get("root_cause_hint")
    if root_cause:
        blocks.insert(
            2,
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": f"*Likely root cause:* {root_cause}"},
            },
        )

    # A card that was paged out through a fallback while this channel was down
    # says so, so the responder knows the PagerDuty alert and this message are
    # the same incident rather than two.
    if payload.get("delivered_via_fallback"):
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            "⚠️ Paged via *"
                            f"{payload['delivered_via_fallback']}* while Slack was "
                            "unreachable — this is the same incident, not a new one."
                        ),
                    }
                ],
            }
        )

    if resolved:
        # An inferred close is not the same claim as a confirmed one, and the
        # card must not let a responder mistake the two.
        if payload.get("resolution_source") == "inferred_silence":
            blocks.append(
                {
                    "type": "section",
                    "text": {
                        "type": "mrkdwn",
                        "text": (
                            "🕓 *Presumed resolved* — alerts stopped arriving. "
                            "Not confirmed by a human."
                        ),
                    },
                }
            )
        else:
            who = payload.get("acknowledged_by")
            via = payload.get("via")
            attribution = ""
            if who and via:
                attribution = f" by {who} in {via}"
            elif via:
                attribution = f" in {via}"
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"✅ *Resolved*{attribution}"},
                }
            )
    elif payload.get("acknowledged_by"):
        blocks.append(
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": (
                            f"👤 Acknowledged by *{payload['acknowledged_by']}*"
                            f" in {payload.get('via', 'unknown')}"
                        ),
                    }
                ],
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
                    },
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "Resolve"},
                        "action_id": "resolve_incident",
                        "style": "primary",
                        "value": payload.get("incident_id", ""),
                    },
                ],
            }
        )

    return blocks


def _build_digest_blocks(payload: dict) -> list[dict]:
    """Render the one message that explains a delivery gap in the channel."""

    minutes = max(1, round(payload.get("duration_seconds", 0) / 60))
    lines = [
        f"*{payload.get('incidents_touched', 0)}* incidents changed while this "
        f"channel was unreachable",
        f"*{payload.get('critical_incidents', 0)}* were critical · "
        f"*{payload.get('resolved_during_outage', 0)}* resolved on their own",
        f"*{payload.get('collapsed_messages', 0)}* redundant updates were "
        f"collapsed instead of replayed",
    ]
    if payload.get("delivered_via_fallback"):
        lines.append(
            f"*{payload['delivered_via_fallback']}* urgent alerts were already "
            f"delivered through a fallback channel"
        )

    return [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔌 Slack delivery restored after {minutes} min",
                "emoji": True,
            },
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {
            "type": "context",
            "elements": [
                {
                    "type": "mrkdwn",
                    "text": (
                        f"Gap: {payload.get('outage_started_at', '?')} → "
                        f"{payload.get('recovered_at', '?')} · "
                        "current state for each incident follows"
                    ),
                }
            ],
        },
    ]


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {settings.SLACK_BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _raise_for_slack_error(error_code: str) -> None:
    """Turn a Slack ``ok: false`` code into a typed, classifiable failure."""

    verdict = classify_slack_error(error_code)
    if verdict.kind is FailureKind.MESSAGE_FATAL:
        raise MessageRejected(verdict.reason)
    if verdict.kind is FailureKind.CHANNEL_DOWN:
        raise ChannelUnavailable(verdict.reason)
    raise RuntimeError(f"Slack API error: {error_code}")


async def _post(client: httpx.AsyncClient, url: str, body: dict) -> dict:
    response = await client.post(url, headers=_headers(), json=body)

    if response.status_code == 429:
        raise RateLimited(float(response.headers.get("Retry-After", "1")))

    # No in-client retry loop: the outbox is the retry mechanism. Blocking the
    # worker here for up to 30s per row would stall every other channel during
    # exactly the outage this design is meant to survive.
    response.raise_for_status()
    data = response.json()

    if not data.get("ok"):
        _raise_for_slack_error(str(data.get("error", "unknown")))

    return data


async def probe() -> None:
    """Cheap liveness check used to detect that Slack has come back.

    ``auth.test`` writes nothing and posts nothing, so the breaker can poll a
    dead channel on a short interval without any risk of spamming a real
    channel or paging anyone. Raising means still down.
    """

    async with httpx.AsyncClient(timeout=5.0) as client:
        response = await client.post(f"{SLACK_API_URL}/auth.test", headers=_headers())
        if response.status_code == 429:
            raise RateLimited(float(response.headers.get("Retry-After", "1")))
        response.raise_for_status()
        data = response.json()
        if not data.get("ok"):
            _raise_for_slack_error(str(data.get("error", "unknown")))


async def send(action: str, payload: dict, external_ref: str | None) -> str:
    if payload.get("kind") == "recovery_digest":
        async with httpx.AsyncClient(timeout=10.0) as client:
            body = {
                "channel": settings.SLACK_CHANNEL_ID,
                "blocks": _build_digest_blocks(payload),
                "text": "Slack delivery restored",
            }
            data = await _post(client, f"{SLACK_API_URL}/chat.postMessage", body)
            return data["ts"]

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
