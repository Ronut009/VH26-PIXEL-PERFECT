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


async def _ensure_schema(conn: aiosqlite.Connection) -> None:
    """Apply idempotent schema additions before application work begins."""

    await conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
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
