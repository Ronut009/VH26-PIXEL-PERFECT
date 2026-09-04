# PulseGraph — Dashboard

Next.js console for the incident pipeline: a light operational dashboard over
the live incident stream, the alert-consolidation number the pitch leads with,
the co-occurrence graph, and a read-only path from an incident to the source
that produced it.

## Setup

```powershell
npm install
copy .env.local.example .env.local
# edit .env.local if the backend isn't on 127.0.0.1:8000
npm run dev
```

Open http://localhost:3000. The FastAPI backend (`uvicorn src.main:app --reload`
from the repo root) needs to be running for real data; without it the console
falls back to labelled sample incidents rather than showing a dead page.

## How it's wired

The browser never calls the backend directly. Every request — including the
SSE subscription — goes to a same-origin Route Handler under `src/app/api/`,
which forwards to exactly one FastAPI endpoint on the server. Two reasons:

- **CORS.** The backend sets no CORS headers, so a cross-origin `fetch` or
  `EventSource` aimed at it is simply blocked. Opening it up with
  `Access-Control-Allow-Origin: *` is the wrong fix — that same backend also
  serves privileged GitHub management routes.
- **Secrets.** `GITHUB_ADMIN_TOKEN` authorizes those GitHub routes. It is read
  on the server, attached inside the route handler, and never reaches the
  browser.

| Browser calls (same-origin) | Route handler forwards to | Bearer |
| --- | --- | --- |
| `GET /api/stream?after=` | `GET /v1/stream` | no |
| `GET /api/health` | `GET /v1/health` | no |
| `GET /api/incidents/recent?since=` | `GET /v1/incidents/recent` | no |
| `GET /api/incidents/edges` | `GET /v1/edges/recent` | no |
| `GET /api/github/install-url` | `GET /v1/github/install-url` | yes |
| `GET /api/github/repositories` | `GET /v1/github/repositories` | yes |
| `POST /api/github/installations/{id}/sync` | `POST /v1/github/installations/{id}/sync` | yes |
| `PUT /api/github/service-mappings/{service}` | `PUT /v1/github/service-mappings/{service}` | yes |
| `POST /api/github/repositories/{id}/snapshots` | `POST /v1/github/repositories/{id}/snapshots` | yes |
| `GET /api/github/snapshots/{id}` | `GET /v1/github/snapshots/{id}` | yes |
| `POST` / `GET /api/github/incidents/{id}/diagnoses` | same on `/v1/github/...` | yes |
| `GET /api/github/analyses/{id}` | `GET /v1/github/analyses/{id}` | yes |
| `POST /api/github/analyses/{id}/patch-preview` | `POST /v1/github/analyses/{id}/patch-preview` | yes |

Each handler is a named route with a fixed upstream path and explicit methods
— there is deliberately no catch-all that would let the browser aim the server
at an arbitrary backend URL. Client-supplied `Authorization` headers are never
forwarded.

### Key modules

- `src/server/pulsegraph.ts` — the only module that reads
  `PULSEGRAPH_API_BASE` and `GITHUB_ADMIN_TOKEN`. Server-only; it throws if it
  is ever bundled for the browser. `callBackend` normalizes every failure to
  `{ error: { code, message, upstream_status } }`; `streamBackend` hands an
  SSE body through untouched, with no timeout and the caller's abort signal.
- `src/lib/api.ts` — typed client for the `/api/*` routes. Failures arrive as
  `PulseGraphApiError` with a machine-readable `code`.
- `src/hooks/usePulseGraphStream.ts` — consumes the stream through
  `/api/stream` with the monotonic `streamId` cursor, and degrades to REST
  polling if the stream cannot be established.
- `src/hooks/useBackendHealth.ts` — polls `GET /api/health` so the system
  health readout reports what the backend said rather than inferring it from
  the socket. The outbox worker has no health endpoint, so its row reads
  "Unknown" instead of guessing.
- `src/hooks/useGithubReadiness.ts` — reads the connected repository list once
  and turns every failure into operator-facing copy, which is what gates and
  explains the drawer's "Investigate code" action.

## GitHub investigation

The Code Investigation view and the drawer action are read-only by
construction, and say so:

- The GitHub App holds Metadata and Contents **read** scope on selected
  repositories only.
- A snapshot pins analysis to an immutable commit, tree and blob inventory.
- Diagnoses cite bounded source evidence, and return a stated fallback rather
  than an ungrounded guess.
- A patch preview is a disposable unified diff for a human to read. Nothing in
  PulseGraph can push, commit, branch, merge, or open a pull request.

"Investigate code" is disabled — with the reason shown next to it — when the
admin token is unset, the backend rejects it, the backend is unreachable, no
repositories are connected, the incident's service has no mapped repository,
or the incident is sample data.

## Environment

`.env.local` (see `.env.local.example`). Both values are server-only; neither
carries a `NEXT_PUBLIC_` prefix, and neither appears in the client bundle.

| Variable | Purpose |
| --- | --- |
| `PULSEGRAPH_API_BASE` | Backend origin. Defaults to `http://127.0.0.1:8000`. |
| `GITHUB_ADMIN_TOKEN` | Bearer token for `/v1/github/*`. Must match the backend's `.env`. |

The old `NEXT_PUBLIC_API_BASE` is no longer read: the browser has no use for
the backend's address now that all traffic is same-origin.

## Not built here

- Screens for GitHub setup (installing the app, mapping services, pinning
  snapshots) and for running a diagnosis or reviewing a patch preview. The
  proxy routes and typed client functions for all of them already exist; the
  UI does not, and the Code Investigation view says so rather than offering
  controls that do nothing.
