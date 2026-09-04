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

## Signing in

The dashboard is gated behind GitHub OAuth via Supabase Auth. An anonymous
visitor can only ever reach `/login`; every page and every `/api/*` route
requires a session.

**This is not the same thing as the GitHub App.** They coexist and do different
jobs:

| Mechanism | Answers | Credential |
| --- | --- | --- |
| GitHub **App** installation | "May PulseGraph read this repo?" | `GITHUB_APP_PRIVATE_KEY` |
| GitHub **OAuth** login (Supabase) | "Who is this human?" | Supabase session cookie |

Creating the OAuth app below does not touch the App installation.

### One-time setup

1. **GitHub → Settings → Developer settings → OAuth Apps → New OAuth App.**
   Set the Authorization callback URL to
   `https://<project-ref>.supabase.co/auth/v1/callback`.
2. **Supabase → Authentication → Providers → GitHub.** Enable it and paste the
   Client ID and Client Secret from step 1.
3. **Supabase → Authentication → URL Configuration.** Site URL
   `http://localhost:3000`, and add `http://localhost:3000/**` to the redirect
   allowlist. Supabase rejects any redirect not listed here.
4. **`web/.env.local`.** Copy `NEXT_PUBLIC_SUPABASE_URL` and the anon
   (publishable) key from Supabase → Project Settings → API, then set
   `GITHUB_ALLOWED_LOGINS` to the GitHub usernames allowed in.

### How it is enforced

Three independent layers, because one is not enough:

- `src/proxy.ts` refreshes the Supabase cookie and redirects signed-out page
  requests to `/login`. This is `proxy.ts`, not `middleware.ts` — Next.js 16
  renamed the convention, so guides that say `middleware.ts` are out of date.
- `src/app/page.tsx` re-checks on the server before rendering any incident
  data. The Next docs warn that a proxy matcher change can silently drop
  coverage, so the route that renders the data verifies the session itself.
- Every handler under `src/app/api/` calls `requireDashboardUser()`
  (`src/server/auth.ts`) as its first statement.

`getAuthState()` uses Supabase's `getUser()` rather than `getSession()`:
`getUser()` revalidates the token with Supabase, while `getSession()` trusts
whatever is in the cookie. For an authorization decision the cookie alone is
not good enough.

**`GITHUB_ALLOWED_LOGINS` fails closed.** An unset or empty value admits
nobody. A missing env var must never be the thing that opens the console up.

### What this does and does not protect

Login is enforced by the Next.js server. FastAPI is unchanged and still trusts
`GITHUB_ADMIN_TOKEN`, so **anyone who can reach the backend directly bypasses
sign-in entirely**. Keep the backend bound to localhost. Before exposing it
anywhere, the backend needs to verify the Supabase JWT itself.

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

"Investigate code" in the incident drawer is disabled — with the reason shown
next to it — when the admin token is unset, the backend rejects it, the backend
is unreachable, no repositories are connected, the incident's service has no
mapped repository, or the incident is sample data. When it is enabled it hands
the incident to the Code Investigation view.

That view carries the whole workflow:

1. **Connect** — fetch the App's install URL, and refresh an installation's
   selected repository list.
2. **Map** — bind a monitored service to a repository. Completions come from
   the service names that actually appear on incident titles, since the backend
   looks the mapping up by that exact name.
3. **Pin** — snapshot a repository, then read back the commit, tree and file
   inventory that a diagnosis will be limited to.
4. **Diagnose** — run a bounded diagnosis for the selected incident and read
   the hypothesis, its per-file and per-line source citations, and the proposed
   fix. A safe fallback is shown as a first-class outcome with its reason and
   next steps, because that is what the backend returns instead of an
   unverified claim.
5. **Review** — generate a unified diff and read it. Nothing applies it.

## Environment

`.env.local` (see `.env.local.example`). Both values are server-only; neither
carries a `NEXT_PUBLIC_` prefix, and neither appears in the client bundle.

| Variable | Purpose |
| --- | --- |
| `PULSEGRAPH_API_BASE` | Backend origin. Defaults to `http://127.0.0.1:8000`. |
| `GITHUB_ADMIN_TOKEN` | Bearer token for `/v1/github/*`. Must match the backend's `.env`. |
| `GITHUB_ALLOWED_LOGINS` | Comma-separated GitHub usernames allowed to sign in. Empty admits nobody. |

`NEXT_PUBLIC_SUPABASE_URL` and `NEXT_PUBLIC_SUPABASE_ANON_KEY` are the two
deliberately public values: they identify the Supabase project but grant
nothing on their own. Access is decided by the session cookie and the
server-side allowlist.

The old `NEXT_PUBLIC_API_BASE` is no longer read: the browser has no use for
the backend's address now that all traffic is same-origin.

## Not built here

- Backend-side verification of the dashboard session. FastAPI still trusts
  `GITHUB_ADMIN_TOKEN`; the Supabase session is checked by the Next.js server
  in front of it.
- Editing a patch preview, or any path that writes it anywhere. The backend
  has no endpoint for it and this app adds none.
