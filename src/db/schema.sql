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
    stable_fingerprint TEXT NOT NULL DEFAULT '',
    scope_key       TEXT NOT NULL DEFAULT '',
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
    bypassed        INTEGER NOT NULL DEFAULT 0, -- 0/1, critical-bypass flag
    bypass_reason   TEXT,
    decision_payload_json TEXT NOT NULL DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_raw_events_seq         ON raw_events(seq);
CREATE INDEX IF NOT EXISTS idx_raw_events_fingerprint ON raw_events(fingerprint);
CREATE INDEX IF NOT EXISTS idx_raw_events_service     ON raw_events(service);
CREATE INDEX IF NOT EXISTS idx_raw_events_fired_at    ON raw_events(fired_at DESC);
CREATE INDEX IF NOT EXISTS idx_raw_events_incident    ON raw_events(incident_id);
CREATE INDEX IF NOT EXISTS idx_raw_events_scope_stable
    ON raw_events(scope_key, stable_fingerprint);

-- ─────────────────────────────────────────────────────────────
-- incidents: lifecycle state for a group of related alerts
-- Owned by Vansh's IncidentEngine, written via DbWriter
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incidents (
    incident_id     TEXT PRIMARY KEY,           -- UUID v4
    scope_key       TEXT NOT NULL DEFAULT '',
    stable_fingerprint TEXT NOT NULL DEFAULT '',
    title           TEXT NOT NULL,
    summary         TEXT,                       -- Vansh fills; may be updated
    severity        TEXT NOT NULL,              -- critical | high | medium | low
    status          TEXT NOT NULL
        CHECK (status IN ('OPEN', 'ACKNOWLEDGED', 'QUIESCENT', 'RESOLVED')),
    alert_count     INTEGER NOT NULL DEFAULT 1,
    first_alert_at  TEXT NOT NULL,
    last_alert_at   TEXT NOT NULL,
    ewma_rate       REAL NOT NULL DEFAULT 0.0,  -- Vansh's EWMA burst signal
    quiet_at_ms     INTEGER,
    ewma_mean_gap   REAL NOT NULL DEFAULT 0.0,
    ewma_variance   REAL NOT NULL DEFAULT 0.0,
    gap_history_json TEXT NOT NULL DEFAULT '[]',
    route_decision  TEXT,                       -- slack | pagerduty | email | suppressed
    root_cause_hint TEXT,                       -- from Anish's graph ranking
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_incidents_status    ON incidents(status);
CREATE INDEX IF NOT EXISTS idx_incidents_severity  ON incidents(severity);
CREATE INDEX IF NOT EXISTS idx_incidents_updated   ON incidents(updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_incidents_scope_stable
    ON incidents(scope_key, stable_fingerprint);
CREATE INDEX IF NOT EXISTS idx_incidents_quiet_deadline
    ON incidents(status, quiet_at_ms);

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

-- delivery_intents: immutable, idempotent write-ahead records for external delivery.
CREATE TABLE IF NOT EXISTS delivery_intents (
    delivery_intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id     TEXT NOT NULL,
    event_id        TEXT NOT NULL,
    channel         TEXT NOT NULL,
    action          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    payload_json    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_delivery_intents_incident
    ON delivery_intents(incident_id);
CREATE INDEX IF NOT EXISTS idx_delivery_intents_pending
    ON delivery_intents(status, created_at);

-- ─────────────────────────────────────────────────────────────
-- GitHub Phase 1: read-only repository connection and snapshots.
-- Source contents are deliberately not stored here. Snapshots pin a commit
-- and immutable Git object IDs so later analysis is reproducible without
-- granting the GitHub App any write capability.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS github_installations (
    installation_id        INTEGER PRIMARY KEY,
    account_login          TEXT NOT NULL,
    account_type           TEXT NOT NULL,
    repository_selection   TEXT NOT NULL DEFAULT 'selected',
    status                 TEXT NOT NULL DEFAULT 'active',
    suspended_at           TEXT,
    permissions_json       TEXT NOT NULL DEFAULT '{}',
    installed_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- Provider event ordering is kept separately so this remains an additive
-- migration for databases that already contain the Phase 1 installation table.
-- GitHub can deliver signed webhooks out of order; this monotonic record stops
-- old lifecycle/selection payloads from restoring access after revocation.
CREATE TABLE IF NOT EXISTS github_installation_state_versions (
    installation_id          INTEGER PRIMARY KEY,
    provider_updated_at_us   INTEGER NOT NULL,
    provider_event_priority  INTEGER NOT NULL,
    revision                 INTEGER NOT NULL DEFAULT 1 CHECK (revision > 0),
    updated_at               TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (installation_id) REFERENCES github_installations(installation_id)
);

CREATE TABLE IF NOT EXISTS github_repositories (
    repository_id          INTEGER PRIMARY KEY,
    installation_id        INTEGER NOT NULL,
    owner                  TEXT NOT NULL,
    name                   TEXT NOT NULL,
    full_name              TEXT NOT NULL UNIQUE,
    default_branch         TEXT NOT NULL,
    html_url               TEXT,
    is_private             INTEGER NOT NULL DEFAULT 1 CHECK (is_private IN (0, 1)),
    is_archived            INTEGER NOT NULL DEFAULT 0 CHECK (is_archived IN (0, 1)),
    is_selected            INTEGER NOT NULL DEFAULT 1 CHECK (is_selected IN (0, 1)),
    last_seen_commit_sha   TEXT,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (installation_id) REFERENCES github_installations(installation_id)
);

CREATE INDEX IF NOT EXISTS idx_github_repositories_installation
    ON github_repositories(installation_id, is_selected);

CREATE TABLE IF NOT EXISTS github_service_mappings (
    service                TEXT PRIMARY KEY,
    repository_id          INTEGER NOT NULL,
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    FOREIGN KEY (repository_id) REFERENCES github_repositories(repository_id)
);

CREATE INDEX IF NOT EXISTS idx_github_service_mappings_repository
    ON github_service_mappings(repository_id);

CREATE TABLE IF NOT EXISTS github_snapshots (
    snapshot_id            TEXT PRIMARY KEY,
    repository_id          INTEGER NOT NULL,
    ref                    TEXT NOT NULL,
    commit_sha             TEXT NOT NULL,
    tree_sha               TEXT NOT NULL,
    file_count             INTEGER NOT NULL DEFAULT 0,
    tree_truncated         INTEGER NOT NULL DEFAULT 0 CHECK (tree_truncated IN (0, 1)),
    created_at             TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE (repository_id, commit_sha),
    FOREIGN KEY (repository_id) REFERENCES github_repositories(repository_id)
);

CREATE INDEX IF NOT EXISTS idx_github_snapshots_repository_created
    ON github_snapshots(repository_id, created_at DESC);

CREATE TABLE IF NOT EXISTS github_snapshot_files (
    snapshot_id            TEXT NOT NULL,
    path                   TEXT NOT NULL,
    blob_sha               TEXT NOT NULL,
    mode                   TEXT NOT NULL,
    object_type            TEXT NOT NULL,
    size_bytes             INTEGER,
    PRIMARY KEY (snapshot_id, path),
    FOREIGN KEY (snapshot_id) REFERENCES github_snapshots(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_github_snapshot_files_blob
    ON github_snapshot_files(blob_sha);

CREATE TABLE IF NOT EXISTS github_webhook_deliveries (
    delivery_id            TEXT PRIMARY KEY,
    event_type             TEXT NOT NULL,
    action                 TEXT,
    installation_id        INTEGER,
    payload_sha256         TEXT NOT NULL,
    processing_status      TEXT NOT NULL DEFAULT 'accepted',
    received_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    processed_at           TEXT
);

CREATE INDEX IF NOT EXISTS idx_github_webhook_deliveries_installation
    ON github_webhook_deliveries(installation_id, received_at DESC);

-- ─────────────────────────────────────────────────────────────
-- GitHub analysis results: append-only, sanitized diagnosis records.
-- These rows intentionally omit source excerpts, GitHub tokens, raw source
-- code, patches, and raw provider payloads. They retain only reproducible
-- snapshot identity, bounded context metadata, and reviewable conclusions.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS github_incident_analyses (
    analysis_id                    TEXT PRIMARY KEY,       -- UUID v4
    incident_id                    TEXT NOT NULL,
    service                        TEXT NOT NULL,
    repository_id                  INTEGER NOT NULL,
    snapshot_id                    TEXT NOT NULL,
    status                         TEXT NOT NULL
        CHECK (status IN ('diagnosed', 'fallback')),
    provider                       TEXT NOT NULL,
    confidence                     REAL NOT NULL
        CHECK (confidence >= 0.0 AND confidence <= 1.0),

    root_cause_summary             TEXT,
    root_cause_reasoning           TEXT,
    evidence_json                  TEXT NOT NULL DEFAULT '[]',
    proposed_fix_summary           TEXT,
    proposed_fix_steps_json        TEXT NOT NULL DEFAULT '[]',
    proposed_fix_paths_json        TEXT NOT NULL DEFAULT '[]',
    fallback_reason                TEXT,
    fallback_message               TEXT,
    fallback_next_steps_json       TEXT NOT NULL DEFAULT '[]',

    source_context_digest          TEXT NOT NULL,           -- metadata-only SHA-256
    source_excerpt_count           INTEGER NOT NULL DEFAULT 0
        CHECK (source_excerpt_count >= 0),
    source_bytes                   INTEGER NOT NULL DEFAULT 0
        CHECK (source_bytes >= 0),
    created_at                     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),

    FOREIGN KEY (incident_id) REFERENCES incidents(incident_id),
    FOREIGN KEY (repository_id) REFERENCES github_repositories(repository_id),
    FOREIGN KEY (snapshot_id) REFERENCES github_snapshots(snapshot_id)
);

CREATE INDEX IF NOT EXISTS idx_github_incident_analyses_incident_created
    ON github_incident_analyses(incident_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_github_incident_analyses_snapshot
    ON github_incident_analyses(snapshot_id, created_at DESC);
