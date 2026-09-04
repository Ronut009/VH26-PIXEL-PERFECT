import json
import os
import sqlite3
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402

FIXTURE_PATH = ROOT / "tests" / "fixtures" / "prometheus_sample.json"
INGEST_URL = "http://localhost:8000/v1/ingest/prometheus"



def ingest_headers() -> dict[str, str]:
    """Credential for the ingest endpoint.

    Ingest authenticates by default, so a script that posts alerts must present
    a token exactly as Alertmanager would. Taken from INGEST_TOKEN, else the
    first entry of the backend's own INGEST_TOKENS, so a working .env needs no
    extra setup here.
    """

    token = os.environ.get("INGEST_TOKEN", "").strip()
    if not token:
        first = settings.INGEST_TOKENS.split(",")[0].strip()
        parts = first.split(":")
        token = parts[1].strip() if len(parts) >= 2 else ""
    if not token:
        print(
            "  ! No ingest token found. Set INGEST_TOKEN, or INGEST_TOKENS in .env.",
            file=sys.stderr,
        )
    return {"Authorization": f"Bearer {token}"} if token else {}


def main() -> None:
    payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))

    print(f"POSTing fixture ({len(payload['alerts'])} alerts) to {INGEST_URL} ...")
    response = httpx.post(
        INGEST_URL, json=payload, headers=ingest_headers(), timeout=10.0
    )
    print(f"HTTP {response.status_code}")
    print(json.dumps(response.json(), indent=2))

    if response.status_code != 200:
        print("FAIL: non-200 response")
        raise SystemExit(1)

    db_path = ROOT / settings.DATABASE_PATH
    conn = sqlite3.connect(str(db_path))
    try:
        raw_events_count = conn.execute("SELECT COUNT(*) FROM raw_events").fetchone()[0]
        incidents_count = conn.execute("SELECT COUNT(*) FROM incidents").fetchone()[0]
        outbox_count = conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]
    finally:
        conn.close()

    print(f"raw_events: {raw_events_count}, incidents: {incidents_count}, outbox: {outbox_count}")

    ok = raw_events_count >= 3 and 1 <= incidents_count <= 3 and 1 <= outbox_count <= 3

    if ok:
        print("PASS")
    else:
        print("FAIL: unexpected row counts")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
