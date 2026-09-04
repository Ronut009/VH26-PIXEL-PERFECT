import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import aiosqlite
import pytest
import pytest_asyncio

from src.contracts import NormalizedEvent
from src.db.hashchain import GENESIS_HASH, canonical_json, compute_row_hash, next_seq_and_prev_hash

SCHEMA_PATH = Path(__file__).parent.parent / "src" / "db" / "schema.sql"


def _make_event(alertname: str) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=uuid4(),
        fingerprint=f"fp-{alertname}",
        source="prometheus",
        service="payment-api",
        alertname=alertname,
        severity_raw="warning",
        status="firing",
        labels={"alertname": alertname, "service": "payment-api"},
        message=f"{alertname} firing",
        fired_at=datetime.now(timezone.utc),
        raw_payload={"alertname": alertname},
    )


async def _insert_raw_event(conn: aiosqlite.Connection, event: NormalizedEvent) -> None:
    seq, prev_hash = await next_seq_and_prev_hash(conn)
    row_hash = compute_row_hash(prev_hash, canonical_json(event))

    await conn.execute(
        """
        INSERT INTO raw_events (
            event_id, seq, fingerprint, source, service, alertname,
            severity_raw, status, labels_json, message, fired_at,
            raw_payload, prev_hash, row_hash, incident_id, is_duplicate, bypassed
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, 0, 0)
        """,
        (
            str(event.event_id),
            seq,
            event.fingerprint,
            event.source,
            event.service,
            event.alertname,
            event.severity_raw,
            event.status,
            json.dumps(event.labels),
            event.message,
            event.fired_at.isoformat(),
            json.dumps(event.raw_payload),
            prev_hash,
            row_hash,
        ),
    )
    await conn.commit()


@pytest_asyncio.fixture
async def db_conn(tmp_path):
    db_path = tmp_path / "test_alerts.db"
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_genesis_row_uses_genesis_prev_hash(db_conn):
    seq, prev_hash = await next_seq_and_prev_hash(db_conn)
    assert seq == 1
    assert prev_hash == GENESIS_HASH


@pytest.mark.asyncio
async def test_chain_grows_correctly_across_inserts(db_conn):
    events = [_make_event(f"Alert{i}") for i in range(3)]
    for event in events:
        await _insert_raw_event(db_conn, event)

    async with db_conn.execute("SELECT * FROM raw_events ORDER BY seq ASC") as cursor:
        rows = await cursor.fetchall()

    assert len(rows) == 3
    assert rows[0]["prev_hash"] == GENESIS_HASH
    assert rows[1]["prev_hash"] == rows[0]["row_hash"]
    assert rows[2]["prev_hash"] == rows[1]["row_hash"]


@pytest.mark.asyncio
async def test_recomputed_hash_matches_stored_hash(db_conn):
    events = [_make_event(f"Alert{i}") for i in range(3)]
    for event in events:
        await _insert_raw_event(db_conn, event)

    async with db_conn.execute("SELECT * FROM raw_events ORDER BY seq ASC") as cursor:
        rows = await cursor.fetchall()

    for row in rows:
        event = NormalizedEvent(
            event_id=row["event_id"],
            fingerprint=row["fingerprint"],
            source=row["source"],
            service=row["service"],
            alertname=row["alertname"],
            severity_raw=row["severity_raw"],
            status=row["status"],
            labels=json.loads(row["labels_json"]),
            message=row["message"],
            fired_at=row["fired_at"],
            raw_payload=json.loads(row["raw_payload"]),
        )
        recomputed = compute_row_hash(row["prev_hash"], canonical_json(event))
        assert recomputed == row["row_hash"]


@pytest.mark.asyncio
async def test_tampering_a_row_breaks_verification(db_conn):
    events = [_make_event(f"Alert{i}") for i in range(2)]
    for event in events:
        await _insert_raw_event(db_conn, event)

    await db_conn.execute(
        "UPDATE raw_events SET message = 'TAMPERED' WHERE seq = 1"
    )
    await db_conn.commit()

    async with db_conn.execute("SELECT * FROM raw_events WHERE seq = 1") as cursor:
        row = await cursor.fetchone()

    event = NormalizedEvent(
        event_id=row["event_id"],
        fingerprint=row["fingerprint"],
        source=row["source"],
        service=row["service"],
        alertname=row["alertname"],
        severity_raw=row["severity_raw"],
        status=row["status"],
        labels=json.loads(row["labels_json"]),
        message=row["message"],
        fired_at=row["fired_at"],
        raw_payload=json.loads(row["raw_payload"]),
    )
    recomputed = compute_row_hash(row["prev_hash"], canonical_json(event))
    assert recomputed != row["row_hash"]
