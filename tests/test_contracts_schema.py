from pathlib import Path
import sqlite3


def test_contract_and_schema_use_uppercase_incident_states() -> None:
    repository_root = Path(__file__).resolve().parents[1]
    contracts = (repository_root / "src" / "contracts.py").read_text(encoding="utf-8")
    schema = (repository_root / "src" / "db" / "schema.sql").read_text(encoding="utf-8")

    for state in ("OPEN", "ACKNOWLEDGED", "QUIESCENT", "RESOLVED"):
        assert f'"{state}"' in contracts
        assert f"'{state}'" in schema

    connection = sqlite3.connect(":memory:")
    connection.executescript(schema)
    incident_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(incidents)").fetchall()
    }

    assert "status" in incident_columns
