# Alert Fatigue Buster — Ingest Spine

Webhook proxy that sits between monitoring tools (Prometheus, Datadog, Grafana) and
notification channels (Slack, PagerDuty, Email). This slice covers **ingest → transactional
write → outbox delivery**: Ingest HTTP Server, EventProcessor/DbWriter, and the Outbox Worker.

Vansh's IncidentEngine and Anish's CoOccurrenceGraph are called through stubs
(`src/stubs.py`) that satisfy the same interface (`src/contracts.py`) — swapping in their
real implementations later requires no changes to `src/db/writer.py`.

## Setup

```powershell
pip install -r requirements.txt
copy .env.example .env
# edit .env: fill in SLACK_BOT_TOKEN, SLACK_CHANNEL_ID, PAGERDUTY_INTEGRATION_KEY
python scripts/init_db.py
```

## Run

Terminal 1:

```powershell
uvicorn src.main:app --reload
```

Terminal 2:

```powershell
python scripts/test_webhook.py
```

Expected: `PASS`, plus 1–3 messages in your Slack channel and (if a Critical alert is in the
fixture) one incident in PagerDuty.

Verify the hash chain (the demo moment):

```powershell
python scripts/verify_chain.py
```

Expected: `CHAIN VALID: N rows verified`.

Health check:

```powershell
curl http://localhost:8000/v1/health
curl "http://localhost:8000/v1/incidents/recent?since=2025-01-01T00:00:00Z"
```

## Architecture

```
Prometheus/Datadog/Grafana webhook
        │
        ▼
POST /v1/ingest/prometheus  (src/main.py)
        │  normalize_prometheus() → NormalizedEvent[]
        ▼
DbWriter.process_event()  (src/db/writer.py)
  BEGIN IMMEDIATE
    ├─ hash-chain append (src/db/hashchain.py)
    ├─ critical_bypass()?  ──yes──► minimal incident + pagerduty outbox row
    │         │no
    │         ▼
    ├─ process_incident_fn()   (stub → Vansh's IncidentEngine)
    ├─ update_graph_fn()       (stub → Anish's CoOccurrenceGraph)
    ├─ upsert incidents row
    └─ insert outbox row (channel by severity)
  COMMIT
        │
        ▼
outbox table  ──polled every 500ms──►  OutboxWorker (src/outbox/worker.py)
                                          ├─ slack.py    → chat.postMessage / chat.update
                                          └─ pagerduty.py → Events API v2 trigger/ack/resolve

GET /v1/incidents/recent  ← read-only, polled by Anish's SSE publisher
```

## Interface contract

`src/contracts.py` defines `NormalizedEvent`, `IncidentDecision`, and `GraphUpdate` — the
Pydantic models shared across the team. `DbWriter` is constructed with
`process_incident_fn` and `update_graph_fn` injected:

```python
writer = DbWriter(
    process_incident_fn=stub_process_incident,   # swap for Vansh's process_incident
    update_graph_fn=stub_update_graph,            # swap for Anish's update_graph
)
```

Both functions receive the same `aiosqlite` connection DbWriter is using, so they run
inside its `BEGIN IMMEDIATE` transaction and must do no external I/O.

## Critical-bypass invariant

`src/ingest/policy.py::critical_bypass()` returns `True` when `severity == "critical"` or
`priority == "P0"`. Bypassed events skip the incident engine and co-occurrence graph
entirely — a minimal incident row and a `pagerduty` outbox row are created directly inside
the same transaction. This holds even if IncidentEngine/CoOccurrenceGraph are broken, since
the bypass path never calls them.

## Tests

```powershell
pytest
```

Covers: Prometheus normalization + fingerprinting (`test_ingest.py`), hash-chain growth and
tamper detection (`test_hashchain.py`), and the DbWriter happy path + critical-bypass path
(`test_writer.py`).

## Not built here

- IncidentEngine (exact dedupe, EWMA burst detection, lifecycle transitions, timer wheel) — Vansh
- CoOccurrenceGraph (decayed edges, root-cause ranking) + SSE publisher — Anish
- Next.js dashboard — Ronit

`GET /v1/incidents/recent` is exposed for Anish's SSE publisher to wrap; the SSE layer
itself is his.
