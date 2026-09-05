import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

import aiosqlite

from src.config import settings

PRAGMAS = (
    "PRAGMA journal_mode = WAL",
    "PRAGMA synchronous = NORMAL",
    "PRAGMA foreign_keys = ON",
    "PRAGMA busy_timeout = 5000",
)
SCHEMA_PATH = Path(__file__).with_name("schema.sql")


async def _apply_pragmas(conn: aiosqlite.Connection) -> None:
    for pragma in PRAGMAS:
        await conn.execute(pragma)


# Additive columns for databases created before delivery-resilience landed.
# CREATE TABLE IF NOT EXISTS cannot widen an existing table, so these are
# applied separately and are safe to run on every startup.
_OUTBOX_COLUMN_MIGRATIONS = (
    # Lower number drains first, so a critical page never waits behind a
    # backlog of low-severity noise when a channel comes back up.
    ("priority", "INTEGER NOT NULL DEFAULT 2"),
    # Set when a newer intent for the same incident+channel replaced this row
    # during recovery coalescing, instead of both being delivered.
    ("locked_by", "TEXT"),
    ("locked_until", "TEXT"),
    ("superseded_by", "INTEGER"),
    # Set on a row created by severity-driven failover, pointing at the row on
    # the unavailable primary channel it stood in for.
    ("failover_of", "INTEGER"),
    # Which channel this row was originally destined for before failover.
    ("origin_channel", "TEXT"),
)


async def _table_columns(conn: aiosqlite.Connection, table: str) -> set[str]:
    async with conn.execute(f"PRAGMA table_info({table})") as cursor:
        return {row[1] for row in await cursor.fetchall()}


# How an incident ended, and how we found out. See schema.sql.
_INCIDENT_COLUMN_MIGRATIONS = (
    # Processing-time anchors. Nullable so existing rows migrate without a
    # backfill; every read falls back to the event-time column when absent.
    ("first_ingested_at", "TEXT"),
    ("last_ingested_at", "TEXT"),
    ("correlation_group_id", "TEXT"),
    ("reopen_count", "INTEGER NOT NULL DEFAULT 0"),
    ("flapping_since", "TEXT"),
    ("last_flap_notified_at", "TEXT"),
    ("acknowledged_at", "TEXT"),
    ("acknowledged_by", "TEXT"),
    ("acknowledged_via", "TEXT"),
    ("resolved_at", "TEXT"),
    ("resolved_via", "TEXT"),
    ("resolution_source", "TEXT"),
    ("resolution_detail", "TEXT"),
)

_GRAPH_SCOPE_COLUMN_MIGRATIONS = (
    ("observed_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("ranked_revision", "INTEGER NOT NULL DEFAULT 0"),
    ("ranked_at", "TEXT"),
)

# What the repository's default branch pointed at when the installation was
# last synced. Distinct from `last_seen_commit_sha`, which records the commit
# that was *pinned* - so on its own it can never say that the repository has
# moved on since. Without the two being separate, a stale pin is invisible.
_GITHUB_REPOSITORY_COLUMN_MIGRATIONS = (
    ("head_commit_sha", "TEXT"),
    ("head_checked_at", "TEXT"),
)

_COLUMN_MIGRATIONS = {
    "outbox": _OUTBOX_COLUMN_MIGRATIONS,
    "incidents": _INCIDENT_COLUMN_MIGRATIONS,
    "graph_scope_stats": _GRAPH_SCOPE_COLUMN_MIGRATIONS,
    "github_repositories": _GITHUB_REPOSITORY_COLUMN_MIGRATIONS,
}


async def _ensure_columns(conn: aiosqlite.Connection) -> None:
    for table, migrations in _COLUMN_MIGRATIONS.items():
        existing = await _table_columns(conn, table)
        for column, definition in migrations:
            if column not in existing:
                await conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                )


# Indexes over columns that arrive by migration. These cannot live in
# schema.sql: that script runs *before* ALTER TABLE does, so on an existing
# database the column does not exist yet, the whole script aborts, and it takes
# down the migration that would have added it. The result is a database that
# can never be opened again - which is exactly what happened.
_MIGRATED_COLUMN_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_incidents_last_ingested"
    " ON incidents(status, last_ingested_at)",
    "CREATE INDEX IF NOT EXISTS idx_incidents_group"
    " ON incidents(correlation_group_id)",
    "CREATE INDEX IF NOT EXISTS idx_outbox_pending"
    " ON outbox(status, next_attempt_at, priority, outbox_id)"
    " WHERE status = 'pending'",
    "CREATE INDEX IF NOT EXISTS idx_outbox_lease"
    " ON outbox(locked_until) WHERE status = 'pending'",
)


async def _ensure_indexes(conn: aiosqlite.Connection) -> None:
    for statement in _MIGRATED_COLUMN_INDEXES:
        await conn.execute(statement)


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    """Apply idempotent schema additions before application work begins.

    Order is load-bearing: tables, then the columns migrations add to them,
    then the indexes that reference those columns. Creating an index before its
    column exists aborts the whole script on any database that predates the
    column.
    """

    await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    await _ensure_columns(conn)
    await _ensure_indexes(conn)
    await conn.commit()


class Database:
    """Holds the single persistent writer connection + its lock.

    All writes (DbWriter, Outbox Worker status updates) go through
    `writer_conn` while holding `write_lock`, since SQLite allows only
    one writer at a time. Reads (GET endpoints) open their own short-lived
    connection via `get_reader_connection()` instead of contending on the lock.
    """

    def __init__(self, db_path: str = settings.DATABASE_PATH):
        self.db_path = db_path
        self.writer_conn: aiosqlite.Connection | None = None
        self.write_lock = asyncio.Lock()

    async def connect(self) -> None:
        self.writer_conn = await aiosqlite.connect(self.db_path)
        self.writer_conn.row_factory = aiosqlite.Row
        await _apply_pragmas(self.writer_conn)
        await _ensure_schema(self.writer_conn)

    async def close(self) -> None:
        if self.writer_conn is not None:
            await self.writer_conn.close()
            self.writer_conn = None


@asynccontextmanager
async def get_reader_connection(db_path: str = settings.DATABASE_PATH):
    conn = await aiosqlite.connect(db_path)
    conn.row_factory = aiosqlite.Row
    try:
        await _apply_pragmas(conn)
        yield conn
    finally:
        await conn.close()
