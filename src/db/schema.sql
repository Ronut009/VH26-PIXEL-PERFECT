-- WAL mode + reasonable pragmas for concurrency
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
PRAGMA foreign_keys = ON;
PRAGMA busy_timeout = 5000;

-- ─────────────────────────────────────────────────────────────
-- raw_events: append-only, hash-chained audit log
-- Every alert that ever entered the system, cryptographically chained
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_events (
    event_id        TEXT PRIMARY KEY,           -- UUID v4
    seq             INTEGER NOT NULL,           -- monotonic sequence, per-row +1
    fingerprint     TEXT NOT NULL,              -- sha256(service|alertname|sorted labels)
    source          TEXT NOT NULL,              -- prometheus | datadog | grafana | generic
    service         TEXT NOT NULL,
    alertname       TEXT NOT NULL,
    severity_raw    TEXT NOT NULL,
    status          TEXT NOT NULL,              -- firing | resolved
    labels_json     TEXT NOT NULL,              -- JSON dict
    message         TEXT NOT NULL,
    fired_at        TEXT NOT NULL,              -- ISO8601 UTC
    ingested_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    raw_payload     TEXT NOT NULL,              -- original JSON, for audit

    -- Hash chain: prev_hash = row_hash of previous row; row_hash = sha256(prev_hash || canonical_row_json)
    prev_hash       TEXT NOT NULL,              -- 64 hex chars
    row_hash        TEXT NOT NULL,              -- 64 hex chars

    -- Denormalized decision outcomes, filled by DbWriter after Vansh/Anish return
    incident_id     TEXT,                       -- FK-ish to incidents.incident_id
    is_duplicate    INTEGER NOT NULL DEFAULT 0, -- 0/1
    bypassed        INTEGER NOT NULL DEFAULT 0  -- 0/1, critical-bypass flag
);

CREATE INDEX IF NOT EXISTS idx_raw_events_seq         ON raw_events(seq);
CREATE INDEX IF NOT EXISTS idx_raw_events_fingerprint ON raw_events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_raw_events_service     ON raw_events(service);
CREATE INDEX IF NOT EXISTS idx_raw_events_fired_at    ON raw_events(fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_events_incident    ON raw_events(incident_id);

-- ─────────────────────────────────────────────────────────────
-- incidents: lifecycle state for a group of related alerts
-- Owned by Vansh's IncidentEngine, written via DbWriter
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     TEXT PRIMARY KEY,           -- UUID v4
    title           TEXT NOT NULL,
    summary         TEXT,                       -- Vansh fills; may be updated
    severity        TEXT NOT NULL,              -- critical | high | medium | low
    status          TEXT NOT NULL
        CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'QUIESCENT', 'RESOLVED')),
    alert_count     INTEGER NOT NULL DEFAULT 1,
    first_alert_at  TEXT NOT NULL,
    last_alert_at   TEXT NOT NULL,
    ewma_rate       REAL NOT NULL DEFAULT 0.0,  -- Vansh's EWMA burst signal
    route_decision  TEXT,                       -- slack | pagerduty | email | suppressed
    root_cause_hint TEXT,                       -- from Anish's graph ranking
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_incidents_status    ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_severity  ON incidents(severity);
CREATE INDEX IF NOT EXISTS idx_incidents_updated   ON incidents(updated_at DESC);

-- ─────────────────────────────────────────────────────────────
-- edges: co-occurrence graph, owned by Anish
-- Decayed directed edges between incident_ids (or service pairs)
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS edges (
    src_incident_id TEXT NOT NULL,
    dst_incident_id TEXT NOT NULL,
    weight          REAL NOT NULL DEFAULT 1.0,  -- decayed weight
    last_seen_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (src_incident_id, dst_incident_id)
);

CREATE INDEX IF NOT EXISTS idx_edges_dst  ON edges(dst_incident_id);
CREATE INDEX IF NOT EXISTS idx_edges_seen ON edges(last_seen_at DESC);

-- ─────────────────────────────────────────────────────────────
-- outbox: durable delivery intents. Your Outbox Worker drains this.
-- Written inside the same transaction as raw_events + incidents.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS outbox (
    outbox_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     TEXT NOT NULL,
    channel         TEXT NOT NULL,              -- slack | pagerduty | email
    action          TEXT NOT NULL,              -- create | update | resolve
    payload_json    TEXT NOT NULL,              -- the message body to send
    status          TEXT NOT NULL DEFAULT 'pending',   -- pending | sent | failed | dead
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_error      TEXT,
    external_ref    TEXT,                       -- Slack ts, PD dedup_key, for updates
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sent_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_pending ON outbox(status, next_attempt_at)
    WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS idx_outbox_incident ON outbox(incident_id);
