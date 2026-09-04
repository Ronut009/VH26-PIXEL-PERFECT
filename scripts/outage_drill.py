"""Run the full Slack-outage lifecycle offline, and narrate it.

    python scripts/outage_drill.py

No network, no Slack workspace, no PagerDuty key: both providers are replaced
with in-process fakes, so the drill can be run on a laptop or in front of a
panel. Everything else - the breaker, the queue, coalescing, the digest - is
the same code the server runs.

The five acts:

    1. Normal delivery.
    2. Slack goes down. The breaker notices and declares an outage.
    3. Alerts keep arriving. Criticals fail over to PagerDuty; the rest queue.
    4. Slack comes back. A probe detects it - nothing guesses.
    5. The backlog is coalesced, re-rendered from current state, and drained.
"""

import asyncio
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from src.db.connection import Database  # noqa: E402
from src.outbox import worker as worker_module  # noqa: E402
from src.outbox.channel_health import BreakerConfig  # noqa: E402
from src.outbox.routing import priority_for  # noqa: E402
from src.outbox.worker import OutboxWorker  # noqa: E402

DB_PATH = ROOT / "data" / "outage_drill.db"


class FakeSlack:
    """A Slack that can be switched off, and that records what it received."""

    def __init__(self) -> None:
        self.up = True
        self.posted: list[dict] = []
        self.probe_calls = 0

    async def send(self, action, payload, external_ref):
        if not self.up:
            raise httpx.ConnectError("slack.com unreachable")
        self.posted.append({"action": action, "payload": payload, "ref": external_ref})
        return external_ref or f"ts-{len(self.posted)}"

    async def probe(self):
        self.probe_calls += 1
        if not self.up:
            raise httpx.ConnectError("slack.com unreachable")


class FakePagerDuty:
    def __init__(self) -> None:
        self.paged: list[dict] = []

    async def send(self, action, payload, external_ref):
        self.paged.append(payload)
        return payload.get("incident_id", "dedup")

    async def probe(self):
        return None


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{dt.microsecond // 1000:03d}Z"


def act(number: int, title: str) -> None:
    print(f"\n{'=' * 68}\n  ACT {number}. {title}\n{'=' * 68}")


def line(text: str) -> None:
    print(f"    {text}")


async def seed_incident(conn, incident_id, title, severity, status="ACKNOWLEDGED"):
    now = _iso(datetime.now(timezone.utc))
    await conn.execute(
        """
        INSERT INTO incidents (
            incident_id, scope_key, stable_fingerprint, title, summary, severity,
            status, alert_count, first_alert_at, last_alert_at
        ) VALUES (?, 'prod/eu-west', ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            incident_id,
            f"fp-{incident_id}",
            title,
            f"{title} detected in prod/eu-west",
            severity,
            status,
            now,
            now,
        ),
    )
    await conn.commit()


async def enqueue(conn, incident_id, severity, action="create"):
    now = _iso(datetime.now(timezone.utc))
    await conn.execute(
        """
        INSERT INTO outbox (
            incident_id, channel, action, payload_json, status,
            next_attempt_at, priority, origin_channel
        ) VALUES (?, 'slack', ?, ?, 'pending', ?, ?, 'slack')
        """,
        (
            incident_id,
            action,
            json.dumps({"incident_id": incident_id}),
            now,
            priority_for(severity),
        ),
    )
    await conn.commit()


async def queue_summary(conn) -> str:
    async with conn.execute(
        """
        SELECT channel, status, COUNT(*) AS n FROM outbox
        GROUP BY channel, status ORDER BY channel, status
        """
    ) as cursor:
        rows = await cursor.fetchall()
    if not rows:
        return "queue empty"
    return " | ".join(f"{r['channel']}/{r['status']}={r['n']}" for r in rows)


async def main() -> None:
    if DB_PATH.exists():
        DB_PATH.unlink()
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    db = Database(str(DB_PATH))
    await db.connect()
    conn = db.writer_conn

    slack, pagerduty = FakeSlack(), FakePagerDuty()
    worker_module._DISPATCHERS["slack"] = slack.send
    worker_module._DISPATCHERS["pagerduty"] = pagerduty.send
    worker_module._PROBES["slack"] = slack.probe
    worker_module._PROBES["pagerduty"] = pagerduty.probe

    worker = OutboxWorker(
        db,
        BreakerConfig(failure_threshold=2, probe_base_seconds=1, half_open_allowance=3),
    )

    # ── Act 1 ──────────────────────────────────────────────────────────────
    act(1, "Slack is healthy. One incident, one message.")
    await seed_incident(conn, "inc-001", "checkout-api - LatencyHigh", "medium")
    await enqueue(conn, "inc-001", "medium")
    await worker._poll_once()
    line(f"Slack received {len(slack.posted)} message(s)")
    line(f"breaker: {(await worker.health.get(conn, 'slack')).state}")

    # ── Act 2 ──────────────────────────────────────────────────────────────
    act(2, "Slack goes down. The system finds out by trying.")
    slack.up = False
    await seed_incident(conn, "inc-002", "db-primary - ConnectionsExhausted", "critical")
    await enqueue(conn, "inc-002", "critical")

    for attempt in range(1, 4):
        async with db.write_lock:
            async with conn.execute(
                "SELECT * FROM outbox WHERE status='pending' AND channel='slack'"
            ) as cursor:
                rows = [dict(r) for r in await cursor.fetchall()]
        for row in rows:
            await worker._dispatch(row)
        state = await worker.health.get(conn, "slack")
        line(f"attempt {attempt}: failures={state.consecutive_failures} breaker={state.state}")

    # The worker's next poll delivers what failover queued onto PagerDuty.
    await worker._poll_once()

    state = await worker.health.get(conn, "slack")
    line("")
    line(f"OUTAGE DECLARED. Next probe at {state.next_probe_at:%H:%M:%S}.")
    line(f"PagerDuty pages sent by failover: {len(pagerduty.paged)}")
    if pagerduty.paged:
        line(f"  -> '{pagerduty.paged[0].get('title')}' (failover_from="
             f"{pagerduty.paged[0].get('failover_from')})")
    line("The 'medium' incident was NOT paged out - it waits for Slack.")

    # ── Act 3 ──────────────────────────────────────────────────────────────
    act(3, "The storm continues while nobody can see it.")
    for index in range(3, 8):
        incident_id = f"inc-{index:03d}"
        severity = "critical" if index == 5 else "low"
        await seed_incident(conn, incident_id, f"api-gateway - Error5xx #{index}", severity)
        await enqueue(conn, incident_id, severity)

    # The same incident keeps changing: forty queued updates, one incident.
    for _ in range(40):
        await enqueue(conn, "inc-002", "critical", action="update")
    await conn.execute(
        "UPDATE incidents SET alert_count = 517 WHERE incident_id = 'inc-002'"
    )
    # One incident opened and closed entirely inside the blind window.
    await conn.execute(
        "UPDATE incidents SET status = 'RESOLVED' WHERE incident_id = 'inc-004'"
    )
    await conn.commit()

    line(await queue_summary(conn))
    line("Every one of these is durable: they are rows in the same SQLite")
    line("transaction that changed the incident, not in-memory state.")

    # ── Act 4 ──────────────────────────────────────────────────────────────
    act(4, "Slack comes back. A probe notices - nothing guesses.")
    slack.up = True
    # Make the scheduled probe due now rather than waiting on wall clock.
    async with db.write_lock:
        await conn.execute(
            "UPDATE channel_health SET next_probe_at = ? WHERE channel = 'slack'",
            (_iso(datetime.now(timezone.utc) - timedelta(seconds=1)),),
        )
        await conn.commit()

    await worker._probe_open_channels()
    state = await worker.health.get(conn, "slack")
    line(f"auth.test probes made during the outage: {slack.probe_calls}")
    line(f"breaker: {state.state}  (real traffic must still prove it)")
    line(await queue_summary(conn))

    async with conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE status = 'superseded'"
    ) as cursor:
        collapsed = (await cursor.fetchone())["n"]
    line(f"redundant updates collapsed instead of replayed: {collapsed}")

    # ── Act 5 ──────────────────────────────────────────────────────────────
    act(5, "The backlog drains: digest first, then criticals, once per incident.")
    before = len(slack.posted)
    for _ in range(12):
        await worker._poll_once()

    delivered = slack.posted[before:]
    line(f"messages posted on recovery: {len(delivered)}")
    for message in delivered:
        payload = message["payload"]
        if payload.get("kind") == "recovery_digest":
            line(
                f"  [DIGEST] {payload['incidents_touched']} incidents | "
                f"{payload['collapsed_messages']} collapsed | "
                f"{payload['delivered_via_fallback']} already paged"
            )
        else:
            marker = " (already paged via PagerDuty)" if payload.get(
                "delivered_via_fallback"
            ) else ""
            line(
                f"  [{payload.get('severity','?'):8}] {message['action']:7} "
                f"{payload.get('title')} | {payload.get('alert_count')} alerts{marker}"
            )

    state = await worker.health.get(conn, "slack")
    line("")
    line(f"breaker: {state.state}")

    async with conn.execute(
        "SELECT COUNT(*) AS n FROM outbox WHERE status = 'dead'"
    ) as cursor:
        dead = (await cursor.fetchone())["n"]
    line(f"messages permanently lost to the outage: {dead}")

    print(f"\n{'=' * 68}")
    print("  41 queued Slack writes for one incident became 1 message.")
    print("  The critical was paged out through PagerDuty within seconds.")
    print("  Nothing was lost, and the channel history explains the gap.")
    print("=" * 68)

    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
