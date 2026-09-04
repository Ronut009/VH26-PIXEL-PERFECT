import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from uuid import UUID

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402
from src.contracts import NormalizedEvent  # noqa: E402
from src.db.hashchain import GENESIS_HASH, canonical_json, compute_row_hash  # noqa: E402


def _row_to_event(row: sqlite3.Row) -> NormalizedEvent:
    return NormalizedEvent(
        event_id=UUID(row["event_id"]),
        fingerprint=row["fingerprint"],
        source=row["source"],
        service=row["service"],
        alertname=row["alertname"],
        severity_raw=row["severity_raw"],
        status=row["status"],
        labels=json.loads(row["labels_json"]),
        message=row["message"],
        fired_at=datetime.fromisoformat(row["fired_at"].replace("Z", "+00:00")),
        raw_payload=json.loads(row["raw_payload"]),
    )


def main() -> None:
    db_path = ROOT / settings.DATABASE_PATH
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row

    try:
        rows = conn.execute(
            "SELECT * FROM raw_events ORDER BY seq ASC"
        ).fetchall()
    finally:
        conn.close()

    if not rows:
        print("CHAIN VALID: 0 rows verified")
        return

    expected_prev_hash = GENESIS_HASH

    for i, row in enumerate(rows):
        if row["prev_hash"] != expected_prev_hash:
            print(f"CHAIN TAMPERED at seq={row['seq']} (event_id={row['event_id']}):")
            print(f"  expected prev_hash={expected_prev_hash}")
            print(f"  stored   prev_hash={row['prev_hash']}")
            raise SystemExit(1)

        event = _row_to_event(row)
        recomputed_hash = compute_row_hash(row["prev_hash"], canonical_json(event))

        if recomputed_hash != row["row_hash"]:
            print(f"CHAIN TAMPERED at seq={row['seq']} (event_id={row['event_id']}):")
            print(f"  stored     row_hash={row['row_hash']}")
            print(f"  recomputed row_hash={recomputed_hash}")
            raise SystemExit(1)

        expected_prev_hash = row["row_hash"]

    print(f"CHAIN VALID: {len(rows)} rows verified")


if __name__ == "__main__":
    main()
