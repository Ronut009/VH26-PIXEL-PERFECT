import hashlib
import json

import aiosqlite

from src.contracts import NormalizedEvent

GENESIS_HASH = "0" * 64


def canonical_json(event: NormalizedEvent) -> str:
    data = event.model_dump(mode="json")
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def compute_row_hash(prev_hash: str, canonical_row_json: str) -> str:
    raw = prev_hash + canonical_row_json
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


async def next_seq_and_prev_hash(db_conn: aiosqlite.Connection) -> tuple[int, str]:
    async with db_conn.execute(
        "SELECT seq, row_hash FROM raw_events ORDER BY seq DESC LIMIT 1"
    ) as cursor:
        row = await cursor.fetchone()

    if row is None:
        return 1, GENESIS_HASH

    return row["seq"] + 1, row["row_hash"]
