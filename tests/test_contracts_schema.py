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
    raw_event_columns = {
        row[1] for row in connection.execute("PRAGMA table_info(raw_events)").fetchall()
    }

    assert {"status", "scope_key", "stable_fingerprint", "quiet_at_ms"}.issubset(
        incident_columns
    )
    assert {
        "scope_key",
        "stable_fingerprint",
        "bypass_reason",
        "decision_payload_json",
    }.issubset(raw_event_columns)
