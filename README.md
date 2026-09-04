# PulseGraph — Adaptive Alert Fatigue Middleware

PulseGraph accepts monitoring webhooks, collapses exact duplicates, predicts a
signal-driven quiet deadline, correlates active incidents through a directed
co-occurrence graph, and emits replay-safe SSE updates for the dashboard.

Fire 500 correlated alerts in 10 seconds → get one consolidated Slack message
with full context, not 500 pings.

## Architecture

```text
Prometheus webhook
      |
      v
FastAPI ingest / NormalizedEvent
      |
      v
DbWriter transaction (BEGIN IMMEDIATE)
  ├─ Engine: dedupe → lifecycle → adaptive EWMA quiet deadline
  ├─ Critical bypass: payment/auth/data-loss → PagerDuty + Slack intents
  ├─ Hash-chained raw-event audit ledger
  └─ Graph: directed co-occurrence edge + root-cause rank
      |
      v
SQLite WAL: incidents, raw_events, edges, delivery_intents, outbox
      |
      +--> SSE: GET /v1/stream → Dashboard
      +--> OutboxWorker → Slack chat.update / PagerDuty
```

Two background asyncio tasks run alongside the HTTP server, sharing one
SQLite writer connection guarded by a single lock (`src/db/connection.py`):

- **TimerWorker** (`src/engine/timer_worker.py`) — polls an in-memory timer
  wheel every 100ms and fires `QUIET_DEADLINE` lifecycle transitions; recovers
  any deadlines that were in flight when the process last stopped.
- **OutboxWorker** (`src/outbox/worker.py`) — polls the `outbox` table every
  `OUTBOX_POLL_INTERVAL_MS` and delivers to Slack/PagerDuty with retry/backoff,
  decoupling "decide what to send" (inside the write transaction) from
  "actually send it" (an unreliable external call).

## Graph evidence contract

**Option B was selected for the hackathon:** no schema migration. The existing
`edges.weight` column is the directed `decayed_joint_weight`, and the SSE
snapshot/delta payload exposes it under that exact name. This preserves the
working SQLite schema while making correlation semantics unambiguous.

## Project structure

```text
src/
├── main.py            FastAPI app, lifespan (startup/shutdown), HTTP routes
├── config.py           pydantic-settings, reads .env
├── contracts.py         shared Pydantic models — the cross-team interface contract
├── ingest/              normalizes vendor webhooks into NormalizedEvent
├── db/                  SQLite schema, connection mgmt, hash chain, DbWriter
├── engine/               IncidentEngine — dedupe, lifecycle, EWMA, critical bypass, timers
├── graph/                CoOccurrenceGraph — decayed edges, root-cause ranking
├── stream/               SSE broker — GET /v1/stream for the live dashboard
├── outbox/               transactional-outbox delivery to Slack/PagerDuty
└── utils/                fingerprinting, structlog config

scripts/    init_db.py, storm_replay.py, verify_chain.py, test_webhook.py
tests/      63 pytest tests mirroring src/'s structure
web/        Next.js dashboard — a separate app, own package.json (see web/README.md)
data/       gitignored — holds alerts.db (SQLite, WAL mode)
```

## Setup — backend

```bash
pip install -r requirements.txt
copy .env.example .env
# fill in SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, PAGERDUTY_INTEGRATION_KEY
python scripts/init_db.py
```

## Setup — dashboard

```bash
cd web
npm install
copy .env.local.example .env.local
npm run dev
```

Open http://localhost:3000. The backend needs to be running for real data;
see `web/README.md` for how the dashboard degrades without it.

## Demo instructions

```bash
# Terminal 1 — start backend
python src/main.py

# Terminal 2 — run the storm replay
# Ingest authenticates, so set a token in .env (INGEST_TOKENS) first; the
# scripts read it from there, or from INGEST_TOKEN if you prefer to export one.
python scripts/storm_replay.py --delay 1

# Terminal 3 (optional) — watch the raw SSE stream
curl -N http://localhost:8000/v1/stream
```

The replay sends ten duplicate alerts, a DB → API → Pod correlated burst, and
a critical payment failure that immediately bypasses aggregation.

## Environment variables

| Variable | Purpose |
|---|---|
| `DATABASE_PATH` | Path to the SQLite file (default `data/alerts.db`) |
| `INGEST_AUTH_ENABLED` | Require a credential on `/v1/ingest/*` (default `true`; fails closed when no tokens are set) |
| `INGEST_TOKENS` | `name:token[:scope]`, comma-separated. Scope is a prefix over `environment/cluster`, so a staging token cannot write production |
| `SLACK_BOT_TOKEN` | Bot token with `chat:write` + `chat:write.public` scopes |
| `SLACK_CHANNEL_ID` | Channel the outbox posts consolidated incidents to |
| `PAGERDUTY_INTEGRATION_KEY` | Events API v2 integration key for critical-bypass paging |
| `LOG_LEVEL` | structlog level (default `INFO`) |
| `ENVIRONMENT` | `dev` / etc., informational |
| `OUTBOX_POLL_INTERVAL_MS` | How often the OutboxWorker polls for pending deliveries (default `500`) |
| `OUTBOX_MAX_ATTEMPTS` | Attempts a row may burn on its *own* faults before `dead` (default `5`). A channel outage no longer consumes this budget |
| `OUTBOX_BREAKER_FAILURE_THRESHOLD` | Consecutive channel-level failures before a channel is declared down (default `3`) |
| `OUTBOX_PROBE_BASE_SECONDS` / `OUTBOX_PROBE_MAX_SECONDS` | Backoff bounds for the liveness probe that detects recovery (default `5` / `120`) |
| `OUTBOX_HALF_OPEN_ALLOWANCE` | Real deliveries trialled before the breaker fully closes (default `3`) |
| `QUIET_WINDOW_MAX_MS` | Ceiling on any single predicted silence window (default `300000`) |
| `INCIDENT_MAX_BATCH_SPAN_MS` | Ceiling on how long one incident may defer delivery, from its first alert (default `600000`) |
| `SLACK_SIGNING_SECRET` | Verifies Slack interaction callbacks; unset means the route rejects everything |
| `PAGERDUTY_WEBHOOK_SECRET` | Verifies PagerDuty v3 webhooks; unset means the route rejects everything |
| `SILENCE_RESOLVE_ENABLED` | Close incidents whose alerts stopped arriving (default `true`) |
| `SILENCE_RESOLVE_MULTIPLIER` / `_CRITICAL_MULTIPLIER` | Silence threshold as a multiple of the incident's own EWMA gap (default `6` / `20`) |
| `SILENCE_RESOLVE_MIN_MS` / `_MAX_MS` | Floor and ceiling on that threshold (default 15 min / 6 h) |
| `SILENCE_SWEEP_INTERVAL_SECONDS` | How often the sweeper looks for gone-quiet incidents (default `30`) |
| `GITHUB_APP_CLIENT_ID` | GitHub App Client ID for the read-only GitHub integration |
| `GITHUB_APP_PRIVATE_KEY` | GitHub App PEM private key (deployment secret) |
| `GITHUB_WEBHOOK_SECRET` | HMAC secret for GitHub App webhook verification |
| `GITHUB_ADMIN_TOKEN` | Temporary protection for GitHub management endpoints until dashboard auth exists |
| `GITHUB_DIAGNOSIS_MAX_*` | Bounded in-memory source context for one incident diagnosis |
| `GITHUB_PATCH_MAX_*` | Bounded local-workspace files, proposal, and diff sizes |
| `OLLAMA_ENABLED` | Enables the optional local-only open-model provider (default `false`) |
| `OLLAMA_MODEL` | Local model name, e.g. `qwen2.5-coder:7b` |
| `OLLAMA_BASE_URL` | Loopback Ollama endpoint only (default `http://127.0.0.1:11434`) |
| `ANTHROPIC_DIAGNOSIS_ENABLED` | Enables the hosted diagnosis tier; sends bounded source off-box, so it is opt-in and loses to Ollama when both are set |
| `ANTHROPIC_API_KEY` / `ANTHROPIC_MODEL` | Credentials and model for hosted diagnosis (default `claude-opus-5`) |
| `SMTP_HOST` / `SMTP_PORT` / `SMTP_USERNAME` / `SMTP_PASSWORD` | SMTP relay for the email failover channel (Gmail needs an App Password) |
| `EMAIL_FROM` / `EMAIL_TO` | Sender and comma-separated recipients for fallback email |
| `OUTBOX_LEASE_SECONDS` / `OUTBOX_BATCH_SIZE` | Delivery claim lease and batch size; the lease is what makes a second worker safe |

Real values live in `.env` (gitignored) — see `.env.example` for the template.
The dashboard has its own `web/.env.local`, with just `NEXT_PUBLIC_API_BASE`
pointing at the backend.

## API reference

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/v1/ingest/prometheus` | Ingest a Prometheus AlertManager webhook payload |
| `POST` | `/v1/github/webhooks` | Verify and process GitHub App installation lifecycle webhooks |
| `GET` | `/v1/github/repositories` | List selected read-only GitHub repositories (admin token required) |
| `POST` | `/v1/github/repositories/{id}/snapshots` | Pin a default-branch source inventory to an immutable commit (admin token required) |
| `POST` | `/v1/github/incidents/{id}/diagnoses` | Produce a bounded, grounded diagnosis or a safe fallback (admin token required) |
| `GET` | `/v1/github/incidents/{id}/diagnoses` | List sanitized diagnosis records for an incident (admin token required) |
| `POST` | `/v1/github/analyses/{id}/patch-preview` | Generate a local-only, human-reviewable diff; never writes GitHub (admin token required) |
| `GET` | `/v1/incidents/recent?since=<iso8601>` | Poll-friendly incident list, consumed by the dashboard |
| `GET` | `/v1/stream[?after=<streamId>]` | Server-Sent Events — snapshot + live deltas (`incident.upsert`, `graph.edge.upsert`, `card.update`, `metrics.update`) |
| `POST` | `/v1/slack/interactions` | Signed Slack interaction callbacks — Acknowledge / Resolve buttons |
| `POST` | `/v1/pagerduty/webhooks` | Signed PagerDuty v3 webhooks — acknowledgement and resolution sync |
| `GET` | `/v1/health` | Liveness check — executes `SELECT 1` against the writer connection |

## Delivery resilience

A Slack outage is a delivery problem here, not a data-loss one. The outbox is
written in the same transaction as the incident, a per-channel circuit breaker
decides whether a channel is up, a side-effect-free probe decides when it is
back, and the backlog is coalesced and re-rendered from current state before it
drains. Critical traffic fails over to PagerDuty while the primary is down.

```bash
python scripts/outage_drill.py     # the whole lifecycle, offline, narrated
```

State also flows back *in*: signed Slack and PagerDuty callbacks apply
acknowledgements and resolutions to the incident, and a sweeper closes
incidents whose alerts simply stopped, since most fixes are never reported by
anyone. Every such change goes through the same hash-chained ledger and outbox
as an alert, so acknowledging in PagerDuty updates the Slack card.

See [Delivery resilience, grouping, and scale](docs/resilience-and-scale.md)
for the mechanisms and their current limits.

## Verification

```bash
pytest tests/ -q --tb=short
python scripts/verify_chain.py
python scripts/check_github_integration.py
python scripts/qwen_diagnosis_demo.py
```

`check_github_integration.py` walks the GitHub feature in the order it has to
work — backend health, admin token, dashboard proxy, App registration,
connected repositories, service mappings, incidents, local model — and prints
the specific thing to change for every step that fails.

`qwen_diagnosis_demo.py` feeds a deliberately broken file and a matching
incident straight into the same local-model provider the API uses, then through
the same patch workspace, and prints the diagnosis and the unified diff. It
needs only Ollama, so the model half can be verified before the GitHub App
exists. Neither script writes anything.

`verify_chain.py` walks `raw_events` in sequence order, recomputes each row's
hash from the previous row's hash plus the canonical event+decision payload,
and confirms every stored hash matches — proving the audit ledger wasn't
tampered with.

## GitHub incident-to-patch MVP

The GitHub integration is deliberately read-only: it accepts signed GitHub App
installation webhooks, maps a monitored service to a selected repository,
pins immutable commit/tree/blob metadata, builds a small source context for a
grounded diagnosis, and can return a disposable local patch preview. It cannot
push, create a branch, pull request, issue, comment, or commit; merge code; or
clone, pull, or modify a repository. See [GitHub Phase 1
setup](docs/github-phase1-setup.md) for GitHub App registration and
[GitHub incident-to-patch MVP](docs/github-integration-mvp.md) for the full
four-phase workflow and local Ollama configuration.

## Ownership

| Slice | Owner | Files |
|---|---|---|
| Ingest HTTP server, DbWriter, Outbox Worker | Yash | `src/main.py` (routes), `src/ingest/`, `src/db/`, `src/outbox/`, `src/utils/` |
| IncidentEngine (dedupe, EWMA, lifecycle, timer wheel) | Vansh | `src/engine/` |
| CoOccurrenceGraph, SSE publisher | Anish | `src/graph/`, `src/stream/` |
| Next.js dashboard | Ronit | `web/` |

`src/contracts.py` is the shared interface every slice depends on — changes
there ripple across the whole team; see its `EngineDecision`/`NormalizedEvent`
models before touching cross-slice code.

The dashboard application is maintained independently in [web/](web/).
