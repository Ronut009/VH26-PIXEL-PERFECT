# Alert Fatigue Buster — Dashboard

Next.js console for the incident pipeline: live incident board, the
alert-volume-cut number the pitch leads with, and a root-cause correlation
graph.

## Setup

```powershell
npm install
copy .env.local.example .env.local
# edit .env.local if the backend isn't on localhost:8000
npm run dev
```

Open http://localhost:3000. The FastAPI backend (`uvicorn src.main:app --reload`
from the repo root) needs to be running for real data; without it the header
shows "backend unreachable" instead of crashing.

## How it's wired

- `src/lib/api.ts` — talks to the backend. `fetchIncidentsSince` hits the
  real `GET /v1/incidents/recent`. `fetchIncidentEdges` calls
  `GET /v1/edges/recent`, which doesn't exist yet (Anish's CoOccurrenceGraph
  slice) — it fails quiet and returns `[]` until that lands.
- `src/hooks/useIncidents.ts` — polls every 4s and merges by `incident_id`.
  This is the only file that needs to change once `GET /v1/stream` (SSE)
  ships: swap the poll loop for an `EventSource`, keep the same
  `{ incidents, connection, lastSync }` return shape.
- `src/components/RootCauseGraph.tsx` — React Flow view of the co-occurrence
  edges. Shows a pending state until edges exist; a hackathon-simple
  circular layout, not a real force layout (fine at incident-graph scale).
- `src/components/VolumePanel.tsx` — the "100 pings → 1 incident" number.
  Derived from data already on the incidents payload
  (`sum(alert_count)` vs. incident count), no extra endpoint needed.

## Not built here

- `GET /v1/stream` (SSE) and `GET /v1/edges/recent` — backend, not this
  slice. The dashboard degrades to polling / an empty graph until they ship.
