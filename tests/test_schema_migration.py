"""Opening a database that predates the current schema.

Every other test builds a fresh database from `schema.sql`, where every column
exists because `CREATE TABLE` declared it. That is structurally unable to catch
the failure that matters most in a deployed system: opening a database that was
created by an *older* version of this code.

It happened. An index was declared in `schema.sql` over a column that arrives
by `ALTER TABLE`. On a fresh database the column exists and the index builds;
on an existing one `CREATE TABLE IF NOT EXISTS` is a no-op, the column is still
missing, and the index statement aborts the whole script - taking down the
migration that would have added the column. The database could never be opened
again, and the entire test suite stayed green.
"""

import os
import tempfile

import aiosqlite
import pytest
import pytest_asyncio

from src.db.connection import _MIGRATED_COLUMN_INDEXES, Database

# The shape of the schema before any of the migrated columns existed: the
# original incidents and outbox tables, and nothing added since.
_LEGACY_SCHEMA = """
CREATE TABLE incidents (
    incident_id     TEXT PRIMARY KEY,
    scope_key       TEXT NOT NULL DEFAULT '',
    stable_fingerprint TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    summary         TEXT,
    severity        TEXT NOT NULL,
    status          TEXT NOT NULL,
    alert_count     INTEGER NOT NULL DEFAULT 1,
    first_alert_at  TEXT NOT NULL,
    last_alert_at   TEXT NOT NULL,
    ewma_rate       REAL NOT NULL DEFAULT 0.0,
    quiet_at_ms     INTEGER,
    ewma_mean_gap   REAL NOT NULL DEFAULT 0.0,
    ewma_variance   REAL NOT NULL DEFAULT 0.0,
    gap_history_json TEXT NOT NULL DEFAULT '[]',
    route_decision  TEXT,
    root_cause_hint TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE outbox (
    outbox_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     TEXT NOT NULL,
    channel         TEXT NOT NULL,
    action          TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_error      TEXT,
    external_ref    TEXT,
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sent_at         TEXT
);
"""


@pytest_asyncio.fixture
async def legacy_db_path():
    """A database file written by a version of this code that predates the
    migrated columns, with a row in it so the upgrade has data to preserve."""

    path = os.path.join(tempfile.mkdtemp(), "legacy.db")
    connection = await aiosqlite.connect(path)
    await connection.executescript(_LEGACY_SCHEMA)
    await connection.execute(
        """
        INSERT INTO incidents (
            incident_id, title, severity, status, first_alert_at, last_alert_at
        ) VALUES ('legacy-1', 'orders-api - LatencyHigh', 'high', 'ACKNOWLEDGED',
                  '2026-09-04T00:00:00.000Z', '2026-09-04T00:00:00.000Z')
        """
    )
    await connection.execute(
        """
        INSERT INTO outbox (incident_id, channel, action, payload_json)
        VALUES ('legacy-1', 'slack', 'create', '{}')
        """
    )
    await connection.commit()
    await connection.close()
    return path


@pytest.mark.asyncio
async def test_an_older_database_still_opens(legacy_db_path):
    """The regression: this raised `no such column: last_ingested_at`."""

    database = Database(legacy_db_path)
    await database.connect()
    try:
        async with database.writer_conn.execute("SELECT 1") as cursor:
            assert await cursor.fetchone() is not None
    finally:
        await database.close()


@pytest.mark.asyncio
async def test_the_upgrade_adds_every_migrated_column(legacy_db_path):
    database = Database(legacy_db_path)
    await database.connect()
    try:
        async with database.writer_conn.execute("PRAGMA table_info(incidents)") as c:
            incident_columns = {row[1] for row in await c.fetchall()}
        async with database.writer_conn.execute("PRAGMA table_info(outbox)") as c:
            outbox_columns = {row[1] for row in await c.fetchall()}
    finally:
        await database.close()

    assert {
        "last_ingested_at",
        "first_ingested_at",
        "correlation_group_id",
        "reopen_count",
        "flapping_since",
    } <= incident_columns
    assert {"priority", "locked_by", "locked_until"} <= outbox_columns


@pytest.mark.asyncio
async def test_the_indexes_over_migrated_columns_are_built(legacy_db_path):
    """Ordering proof: these can only exist if the columns landed first."""

    database = Database(legacy_db_path)
    await database.connect()
    try:
        async with database.writer_conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index'"
        ) as cursor:
            indexes = {row[0] for row in await cursor.fetchall()}
    finally:
        await database.close()

    assert {
        "idx_incidents_last_ingested",
        "idx_incidents_group",
        "idx_outbox_pending",
        "idx_outbox_lease",
    } <= indexes
    assert len(_MIGRATED_COLUMN_INDEXES) == 4


@pytest.mark.asyncio
async def test_existing_rows_survive_the_upgrade(legacy_db_path):
    """A migration that loses data is not a migration."""

    database = Database(legacy_db_path)
    await database.connect()
    try:
        async with database.writer_conn.execute(
            "SELECT title, reopen_count, last_ingested_at FROM incidents"
        ) as cursor:
            row = await cursor.fetchone()
    finally:
        await database.close()

    assert row["title"] == "orders-api - LatencyHigh"
    assert row["reopen_count"] == 0, "new counters start at their default"
    assert row["last_ingested_at"] is None, (
        "an unbackfilled processing-time column is null, and every read of it "
        "falls back to the event-time column"
    )


@pytest.mark.asyncio
async def test_opening_twice_is_idempotent(legacy_db_path):
    """Startup runs this every time, not only on the upgrade."""

    for _ in range(2):
        database = Database(legacy_db_path)
        await database.connect()
        await database.close()
