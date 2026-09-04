"""
Smoke test for Slack and PagerDuty integrations.

Sends a test message to Slack and triggers + resolves a test incident
on PagerDuty to verify both integrations are healthy.

Usage:
    python -m scripts.smoke_test_integrations
"""

import asyncio
import sys
import os
from datetime import datetime, timezone

# Ensure project root is on sys.path so `src.*` imports resolve.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import httpx
from src.config import settings


# ── Colours for terminal output ──────────────────────────────────────────────
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
CYAN = "\033[96m"
BOLD = "\033[1m"
RESET = "\033[0m"


def _ts() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def banner(text: str) -> None:
    print(f"\n{BOLD}{CYAN}{'─' * 60}")
    print(f"  {text}")
    print(f"{'─' * 60}{RESET}\n")


def ok(msg: str) -> None:
    print(f"  {GREEN}✔ {msg}{RESET}")


def fail(msg: str) -> None:
    print(f"  {RED}✘ {msg}{RESET}")


def warn(msg: str) -> None:
    print(f"  {YELLOW}⚠ {msg}{RESET}")


def info(msg: str) -> None:
    print(f"  {CYAN}ℹ {msg}{RESET}")


# ── Slack smoke test ─────────────────────────────────────────────────────────
async def test_slack() -> bool:
    banner("Slack Integration Smoke Test")

    token = settings.SLACK_BOT_TOKEN
    channel = settings.SLACK_CHANNEL_ID

    if not token:
        fail("SLACK_BOT_TOKEN is empty — skipping Slack test.")
        return False
    if not channel:
        fail("SLACK_CHANNEL_ID is empty — skipping Slack test.")
        return False

    ok(f"SLACK_BOT_TOKEN is set (ends …{token[-6:]})")
    ok(f"SLACK_CHANNEL_ID = {channel}")

    # Step 1: auth.test — verify the token is valid
    info("Testing Slack auth (auth.test) …")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    async with httpx.AsyncClient(timeout=15.0) as client:
        try:
            resp = await client.post(
                "https://slack.com/api/auth.test", headers=headers, json={}
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                ok(f"Auth OK — bot user: {data.get('user')} / team: {data.get('team')}")
            else:
                fail(f"auth.test returned ok=false: {data.get('error')}")
                return False
        except Exception as exc:
            fail(f"auth.test failed: {exc}")
            return False

        # Step 2: Send a test message
        info("Posting a smoke-test message …")
        now = _ts()
        blocks = [
            {
                "type": "header",
                "text": {
                    "type": "plain_text",
                    "text": "🧪 PulseGraph Smoke Test",
                    "emoji": True,
                },
            },
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"This is an automated smoke test from *PulseGraph*.\n"
                        f"Timestamp: `{now}`\n\n"
                        f"If you see this message, the Slack integration is ✅ *working*."
                    ),
                },
            },
            {
                "type": "context",
                "elements": [
                    {
                        "type": "mrkdwn",
                        "text": f"smoke test · env={settings.ENVIRONMENT} · {now}",
                    }
                ],
            },
        ]
        body = {
            "channel": channel,
            "blocks": blocks,
            "text": "🧪 PulseGraph Smoke Test",
        }
        try:
            resp = await client.post(
                "https://slack.com/api/chat.postMessage", headers=headers, json=body
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("ok"):
                ts = data.get("ts")
                ok(f"Message posted! ts={ts}")
                return True
            else:
                fail(f"chat.postMessage error: {data.get('error')}")
                if data.get("error") == "channel_not_found":
                    warn("Hint: The bot may not be invited to the channel. Use /invite @bot-name in the channel.")
                if data.get("error") == "not_in_channel":
                    warn("Hint: The bot is not a member of the channel. Use /invite @bot-name.")
                return False
        except Exception as exc:
            fail(f"chat.postMessage failed: {exc}")
            return False


# ── PagerDuty smoke test ─────────────────────────────────────────────────────
async def test_pagerduty() -> bool:
    banner("PagerDuty Integration Smoke Test")

    routing_key = settings.PAGERDUTY_INTEGRATION_KEY

    if not routing_key:
        fail("PAGERDUTY_INTEGRATION_KEY is empty — skipping PagerDuty test.")
        return False

    ok(f"PAGERDUTY_INTEGRATION_KEY is set (ends …{routing_key[-6:]})")

    dedup_key = f"pulsegraph-smoke-test-{_ts()}"

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Step 1: Trigger a test incident
        info("Triggering a test incident …")
        trigger_body = {
            "routing_key": routing_key,
            "event_action": "trigger",
            "dedup_key": dedup_key,
            "payload": {
                "summary": "🧪 PulseGraph Smoke Test — please ignore & auto-resolves",
                "source": "pulsegraph-smoke-test",
                "severity": "info",
                "timestamp": _ts(),
                "custom_details": {
                    "purpose": "Automated integration smoke test",
                    "environment": settings.ENVIRONMENT,
                },
            },
        }
        try:
            resp = await client.post(
                "https://events.pagerduty.com/v2/enqueue", json=trigger_body
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                ok(f"Incident triggered! dedup_key={data.get('dedup_key')}")
            else:
                fail(f"Trigger returned unexpected status: {data}")
                return False
        except httpx.HTTPStatusError as exc:
            fail(f"Trigger HTTP error {exc.response.status_code}: {exc.response.text}")
            if exc.response.status_code == 400:
                warn("Hint: Check that the integration key belongs to an Events API v2 integration.")
            return False
        except Exception as exc:
            fail(f"Trigger failed: {exc}")
            return False

        # Step 2: Immediately resolve it so it doesn't wake anyone up
        info("Resolving the test incident …")
        resolve_body = {
            "routing_key": routing_key,
            "event_action": "resolve",
            "dedup_key": dedup_key,
        }
        try:
            resp = await client.post(
                "https://events.pagerduty.com/v2/enqueue", json=resolve_body
            )
            resp.raise_for_status()
            data = resp.json()
            if data.get("status") == "success":
                ok("Incident resolved!")
                return True
            else:
                fail(f"Resolve returned unexpected status: {data}")
                return False
        except Exception as exc:
            fail(f"Resolve failed: {exc}")
            return False


# ── Main ─────────────────────────────────────────────────────────────────────
async def main() -> None:
    banner("PulseGraph Integration Smoke Test Suite")
    info(f"Environment: {settings.ENVIRONMENT}")
    info(f"Timestamp:   {_ts()}")

    results: dict[str, bool] = {}

    results["Slack"] = await test_slack()
    results["PagerDuty"] = await test_pagerduty()

    # ── Summary ──────────────────────────────────────────────────────────
    banner("Results Summary")
    all_pass = True
    for name, passed in results.items():
        if passed:
            ok(f"{name}: PASS")
        else:
            fail(f"{name}: FAIL")
            all_pass = False

    if all_pass:
        print(f"\n{BOLD}{GREEN}  All integrations are healthy! 🎉{RESET}\n")
    else:
        print(f"\n{BOLD}{RED}  Some integrations failed — check output above.{RESET}\n")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
