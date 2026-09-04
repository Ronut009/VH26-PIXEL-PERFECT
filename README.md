# PulseGraph — Adaptive Alert Fatigue Middleware

PulseGraph accepts monitoring webhooks, collapses exact duplicates, predicts a
signal-driven quiet deadline, correlates active incidents through a directed
co-occurrence graph, and emits replay-safe SSE updates for the dashboard.

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

## Graph evidence contract

**Option B was selected for the hackathon:** no schema migration. The existing
`edges.weight` column is the directed `decayed_joint_weight`, and the SSE
snapshot/delta payload exposes it under that exact name. This preserves the
working SQLite schema while making correlation semantics unambiguous.

## Setup

```bash
pip install -r requirements.txt
copy .env.example .env
python scripts/init_db.py
```

## Demo Instructions

```bash
# Start backend
python src/main.py

# Run storm replay
python scripts/storm_replay.py --delay 1

# Verify SSE stream
curl -N http://localhost:8000/v1/stream
```

The replay sends ten duplicate alerts, a DB → API → Pod correlated burst, and
a critical payment failure that immediately bypasses aggregation.

## Verification

```bash
pytest tests/ -q --tb=short
python scripts/verify_chain.py
```

The dashboard application is maintained independently in [web/](web/).
