-- WAL mode + reasonable pragmas for concurrency
-- NOTE: indexes over columns that arrive by migration (priority, locked_until,
-- correlation_group_id, last_ingested_at) are NOT declared here. This script
-- runs before ALTER TABLE does, so on an existing database those columns do
-- not exist yet and the whole script aborts - taking the migration that would
-- have added them down with it. They are created in src/db/connection.py,
-- after the columns exist. See _MIGRATED_COLUMN_INDEXES.

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
    -- EVENT TIME - what the monitor believes. Used for the ledger and for
    -- measuring inter-arrival gaps, because a source's own clock is the right
    -- measure of its own cadence: a constant offset cancels out in a difference.
    first_alert_at  TEXT NOT NULL,
    last_alert_at   TEXT NOT NULL,
    -- PROCESSING TIME - when we actually saw it. Everything that decides
    -- *elapsed time* uses these: quiet deadlines, silence detection, and the
    -- correlation window. Scheduling must never depend on a third party's
    -- clock; a source running twenty minutes behind would otherwise have its
    -- live incidents auto-resolved and its batching defeated.
    first_ingested_at TEXT,
    last_ingested_at  TEXT,
    ewma_rate       REAL NOT NULL DEFAULT 0.0,  -- Vansh's EWMA burst signal
    quiet_at_ms     INTEGER,
    ewma_mean_gap   REAL NOT NULL DEFAULT 0.0,
    ewma_variance   REAL NOT NULL DEFAULT 0.0,
    gap_history_json TEXT NOT NULL DEFAULT '[]',
    route_decision  TEXT,                       -- slack | pagerduty | email | suppressed
    root_cause_hint TEXT,                       -- from Anish's graph ranking
    -- Provenance for how an incident ended. A fix can reach us three ways:
    -- the monitor says so, a human acted in Slack/PagerDuty, or the alerts
    -- simply stopped and we inferred it. All three must be distinguishable,
    -- because "nobody told us, we guessed" is not the same claim as
    -- "Prometheus sent resolved".
    -- Set when the correlation graph has tied this incident to others in the
    -- same cascade. Members other than the anchor stop posting their own card.
    correlation_group_id TEXT,
    -- Flap damping. The silence sweeper closes a quiet incident and the engine
    -- reopens it on the next alert; both are right, and together a badly
    -- thresholded alert cycles forever, posting a card update every time. The
    -- count is what separates "this resolved and came back" from "this alert
    -- is broken", and the last-notified stamp is what collapses the repeats
    -- into a periodic digest instead of a per-transition stream.
    reopen_count          INTEGER NOT NULL DEFAULT 0,
    flapping_since        TEXT,
    last_flap_notified_at TEXT,
    acknowledged_at   TEXT,
    acknowledged_by   TEXT,                     -- provider user id/name
    acknowledged_via  TEXT,                     -- slack | pagerduty | dashboard
    resolved_at       TEXT,
    resolved_via      TEXT,                     -- slack | pagerduty | dashboard
    resolution_source TEXT,                     -- monitor | operator | inferred_silence
    resolution_detail TEXT,
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
-- source_clock_skew: how far each source's clock sits from ours.
--
-- Every alert carries two timestamps: fired_at (the monitor's clock) and
-- ingested_at (ours). Their difference is drift, and drift used to be
-- invisible - it silently defeated batching and auto-resolved live incidents
-- with nothing in the logs to explain why. A monitoring system that trusts
-- remote clocks without measuring them has a blind spot exactly where it is
-- supposed to have vision, so the drift is now a signal in its own right.
--
-- Negative skew means the source is behind us; positive means it runs ahead.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS source_clock_skew (
    source           TEXT NOT NULL,
    scope_key        TEXT NOT NULL DEFAULT '',
    last_skew_ms     INTEGER NOT NULL DEFAULT 0,
    max_abs_skew_ms  INTEGER NOT NULL DEFAULT 0,
    samples          INTEGER NOT NULL DEFAULT 0,
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (source, scope_key)
);

-- ─────────────────────────────────────────────────────────────
-- graph marginals: how often each node fires, and how many observation
-- rounds a scope has seen.
--
-- `edges.weight` is a raw decayed co-occurrence count, and a raw count cannot
-- tell "these two are related" from "these two are both always firing". Two
-- chronically noisy services co-occur constantly by chance alone. Without the
-- marginals there is no denominator to divide that chance out by, so the
-- ranker treats coincidence and causation as the same evidence.
--
-- These are the P(a) and P(b) terms of lift = P(a,b) / (P(a)P(b)); `rounds` is
-- the N that turns decayed counts into probabilities.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS graph_node_stats (
    incident_id       TEXT PRIMARY KEY,
    scope_key         TEXT NOT NULL DEFAULT '',
    observations      REAL NOT NULL DEFAULT 0.0,   -- decayed rounds this node appeared in
    last_observed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_graph_node_stats_scope
    ON graph_node_stats(scope_key);

CREATE TABLE IF NOT EXISTS graph_scope_stats (
    scope_key         TEXT PRIMARY KEY,
    rounds            REAL NOT NULL DEFAULT 0.0,   -- decayed observation rounds in scope
    last_observed_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    -- Dirtiness is tracked by revision, not by timestamp, on purpose.
    -- last_observed_at carries the monitor's clock (event time) while any
    -- "when did we last rank" stamp is wall clock, and comparing the two means
    -- a source whose clock runs behind marks its scope permanently clean - root
    -- cause would silently stop updating. A counter has no clock to disagree
    -- with. The lag between the two *is* the debounce: a burst of alerts bumps
    -- observed_revision many times and still earns one ranking pass.
    observed_revision INTEGER NOT NULL DEFAULT 0,
    ranked_revision   INTEGER NOT NULL DEFAULT 0,
    ranked_at         TEXT                        -- for humans, never compared
);

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
    -- pending | sent | dead | superseded. 'superseded' means a newer intent for
    -- the same incident replaced this one during recovery coalescing, so it was
    -- deliberately never sent rather than lost.
    status          TEXT NOT NULL DEFAULT 'pending',
    -- Only counts failures that are this row's own fault. A channel outage
    -- parks a row without charging it an attempt, so an outage cannot
    -- dead-letter the backlog just by lasting longer than the retry budget.
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    last_error      TEXT,
    external_ref    TEXT,                       -- Slack ts, PD dedup_key, for updates
    -- Derived from severity at enqueue time; lower drains first, so a backlog
    -- released after an outage delivers criticals before low-severity noise.
    priority        INTEGER NOT NULL DEFAULT 2,
    -- Lease held by the worker currently dispatching this row. A second worker
    -- must be impossible to add safely without this: without a claim, two
    -- workers select the same pending rows and both send them. The lease
    -- expires so a worker that dies mid-dispatch releases its rows instead of
    -- stranding them forever.
    locked_by       TEXT,
    locked_until    TEXT,
    superseded_by   INTEGER,                    -- winning outbox_id, when collapsed
    failover_of     INTEGER,                    -- primary row this stood in for
    origin_channel  TEXT,                       -- channel intended before failover
    created_at      TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    sent_at         TEXT
);

CREATE INDEX IF NOT EXISTS idx_outbox_incident ON outbox(incident_id);

-- ─────────────────────────────────────────────────────────────
-- incident_groups: one storm, not N incidents.
--
-- The co-occurrence graph could already tell that a DB failure, an API
-- failure and a pod failure were one cascade, but it only *annotated* each
-- with a root-cause hint - so a three-service outage still posted three cards
-- and a responder still had to work out they were the same event.
--
-- Two different incident ids matter here and they are deliberately separate:
--
--   anchor_incident_id  owns the Slack message. Fixed for the group's life,
--                       because the card's external_ref (the Slack ts) belongs
--                       to it - if the anchor moved, the card would jump to a
--                       new message mid-incident.
--   root_incident_id    the current ranked cause. Free to change as evidence
--                       accumulates, because it is only rendered, never used
--                       as an identity.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS incident_groups (
    group_id            TEXT PRIMARY KEY,
    scope_key           TEXT NOT NULL,
    anchor_incident_id  TEXT NOT NULL,
    root_incident_id    TEXT,
    title               TEXT NOT NULL DEFAULT '',
    severity            TEXT NOT NULL DEFAULT 'medium',
    member_count        INTEGER NOT NULL DEFAULT 1,
    status              TEXT NOT NULL DEFAULT 'OPEN',
    created_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at          TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_incident_groups_scope
    ON incident_groups(scope_key, status);
CREATE INDEX IF NOT EXISTS idx_incident_groups_anchor
    ON incident_groups(anchor_incident_id);

-- ─────────────────────────────────────────────────────────────
-- inbound_events: every signed callback we accept from a provider.
-- The outbox is how state leaves this system; this is how it comes back.
-- Providers retry deliveries, so the delivery id is the idempotency key:
-- replaying the same Slack click or PagerDuty webhook must not re-transition
-- an incident or append a second ledger entry.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS inbound_events (
    inbound_id      TEXT PRIMARY KEY,           -- provider delivery/trigger id
    provider        TEXT NOT NULL,              -- slack | pagerduty
    kind            TEXT NOT NULL,              -- acknowledge | resolve | unknown
    incident_id     TEXT,
    actor           TEXT,                       -- provider user id, when known
    status          TEXT NOT NULL DEFAULT 'applied',
        -- applied | duplicate | ignored | rejected
    detail          TEXT,
    payload_sha256  TEXT NOT NULL DEFAULT '',
    received_at     TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE INDEX IF NOT EXISTS idx_inbound_events_incident
    ON inbound_events(incident_id, received_at DESC);

-- ─────────────────────────────────────────────────────────────
-- channel_health: one row per delivery channel. This is the system's
-- answer to "how do you know Slack is down, and how do you know it is back?"
-- The OutboxWorker never asks a single failed row whether a channel is
-- healthy; it asks this table, which is driven by a circuit breaker fed by
-- classified failures and cleared only by an explicit, side-effect-free probe.
-- ─────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS channel_health (
    channel              TEXT PRIMARY KEY,          -- slack | pagerduty | email
    state                TEXT NOT NULL DEFAULT 'CLOSED'
        CHECK (state IN ('CLOSED', 'OPEN', 'HALF_OPEN')),
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    probe_backoff_step   INTEGER NOT NULL DEFAULT 0,
    opened_at            TEXT,                      -- outage start, ISO8601
    next_probe_at        TEXT,                      -- when to try auth.test again
    last_success_at      TEXT,
    last_failure_at      TEXT,
    last_error           TEXT,
    outage_count         INTEGER NOT NULL DEFAULT 0,
    updated_at           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

-- channel_outages: closed-book record of every detected outage window, so the
-- recovery digest and the dashboard can say exactly what the blind window was.
CREATE TABLE IF NOT EXISTS channel_outages (
    outage_id       INTEGER PRIMARY KEY AUTOINCREMENT,
    channel         TEXT NOT NULL,
    detected_at     TEXT NOT NULL,
    recovered_at    TEXT,
    probe_attempts  INTEGER NOT NULL DEFAULT 0,
    queued_at_peak  INTEGER NOT NULL DEFAULT 0,
    failed_over     INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT
);

CREATE INDEX IF NOT EXISTS idx_channel_outages_channel
    ON channel_outages(channel, detected_at DESC);

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
