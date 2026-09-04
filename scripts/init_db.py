import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.config import settings  # noqa: E402

SCHEMA_PATH = ROOT / "src" / "db" / "schema.sql"


def main() -> None:
    db_path = ROOT / settings.DATABASE_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)

    schema_sql = SCHEMA_PATH.read_text(encoding="utf-8")

    try:
        conn = sqlite3.connect(str(db_path))
        try:
            conn.executescript(schema_sql)
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        print(f"DB init failed: {exc}")
        raise SystemExit(1)

    print("DB initialized")


if __name__ == "__main__":
    main()
