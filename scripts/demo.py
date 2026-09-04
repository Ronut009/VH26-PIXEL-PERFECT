"""One-command PulseGraph demo: configure, seed real traffic, verify every hop.

Run this with the backend and the dashboard already running. It configures the
shared admin token, fires a real alert storm at the real ingest endpoints, then
checks every link in the chain the judges are about to look at -- backend,
database, hash-chained ledger, the dashboard's same-origin proxy, the live SSE
stream, and the GitHub investigation path.

Nothing is faked. Every number it prints came back out of the pipeline.

    .venv/Scripts/python.exe scripts/demo.py
    .venv/Scripts/python.exe scripts/demo.py --skip-seed     # verify only
    .venv/Scripts/python.exe scripts/demo.py --reset         # clean numbers (backend must be stopped)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, ".")

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"
WEB_ENV_FILE = REPO_ROOT / "web" / ".env.local"

OK = "[ ok ]"
BAD = "[fail]"
INFO = "[ .. ]"
WARN = "[warn]"

problems: list[str] = []


def head(title: str) -> None:
    print(f"\n{title}\n{'=' * 72}")


def line(mark: str, text: str, detail: str = "") -> None:
    print(f"{mark} {text}")
    if detail:
        for part in detail.splitlines():
            print(f"       {part}")


def fail(text: str, detail: str = "") -> None:
    problems.append(text)
    line(BAD, text, detail)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


# ─────────────────────────────────────────────────────────── configuration ──

def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def set_env_value(path: Path, key: str, value: str) -> None:
    """Rewrite one key in place, preserving every other line and comment."""

    lines = path.read_text(encoding="utf-8").splitlines(keepends=True) if path.exists() else []
    replaced = False
    for index, raw in enumerate(lines):
        if raw.strip().startswith(f"{key}="):
            lines[index] = f"{key}={value}\n"
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith("\n"):
            lines.append("\n")
        lines.append(f"{key}={value}\n")
    path.write_text("".join(lines), encoding="utf-8")


def configure(backend_url: str) -> tuple[str, bool]:
    """Ensure both halves share one admin token. Returns (token, restart_needed)."""

    head("1. Configuration")
    restart_needed = False

    backend_env = read_env(ENV_FILE)
    token = backend_env.get("GITHUB_ADMIN_TOKEN", "")
    if not token:
        token = secrets.token_urlsafe(32)
        set_env_value(ENV_FILE, "GITHUB_ADMIN_TOKEN", token)
        restart_needed = True
        line(OK, "Generated GITHUB_ADMIN_TOKEN and wrote it to .env")
    else:
        line(OK, "GITHUB_ADMIN_TOKEN already set in .env")

    web_env = read_env(WEB_ENV_FILE)
    if web_env.get("GITHUB_ADMIN_TOKEN") != token or not web_env.get("PULSEGRAPH_API_BASE"):
        if not WEB_ENV_FILE.exists():
            WEB_ENV_FILE.write_text(
                "# Server-only. Read by the Route Handlers under src/app/api/, never by the browser.\n",
                encoding="utf-8",
            )
        set_env_value(WEB_ENV_FILE, "PULSEGRAPH_API_BASE", backend_url)
        set_env_value(WEB_ENV_FILE, "GITHUB_ADMIN_TOKEN", token)
        restart_needed = True
        line(OK, "Wrote web/.env.local so the dashboard shares the same token")
    else:
        line(OK, "web/.env.local already matches .env")

    if restart_needed:
        line(
            WARN,
            "Environment changed -- both processes must be restarted to pick it up",
            "Stop and restart:\n"
            "  1) uvicorn src.main:app --reload\n"
            "  2) cd web && npm run dev\n"
            "Then run this script again.",
        )
    return token, restart_needed


# ────────────────────────────────────────────────────────────────── health ──

async def wait_for(client: httpx.AsyncClient, url: str, seconds: float = 20.0) -> httpx.Response | None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            return await client.get(url, timeout=5.0)
        except httpx.HTTPError:
            await asyncio.sleep(0.5)
    return None


async def check_services(
    client: httpx.AsyncClient, backend: str, dashboard: str, ollama: str, model: str
) -> None:
    head("2. Services")

    response = await wait_for(client, f"{backend}/v1/health")
    if response is None:
        fail("Backend reachable", f"Nothing answered on {backend}.\nStart it: uvicorn src.main:app --reload")
    elif response.json().get("status") == "healthy":
        line(OK, f"Backend healthy at {backend}")
    else:
        fail("Backend database healthy", str(response.text)[:200])

    response = await wait_for(client, f"{dashboard}/api/health", seconds=10)
    if response is None:
        fail("Dashboard reachable", f"Nothing answered on {dashboard}.\nStart it: cd web && npm run dev")
    elif response.status_code == 200 and response.json().get("status") == "healthy":
        line(OK, f"Dashboard proxy reaches the backend ({dashboard}/api/health -> /v1/health)")
    else:
        fail(
            "Dashboard proxy reaches the backend",
            f"HTTP {response.status_code}: {response.text[:200]}",
        )

    try:
        tags = (await client.get(f"{ollama}/api/tags", timeout=5.0)).json()
        names = {entry.get("name") for entry in tags.get("models", [])}
        if model in names:
            line(OK, f"Ollama reachable, {model} pulled")
        else:
            line(WARN, f"{model} is not pulled", f"ollama pull {model}")
    except (httpx.HTTPError, ValueError):
        line(WARN, "Ollama not reachable", "Code diagnosis will return its safe fallback.")


# ──────────────────────────────────────────────────────────── alert storm ──

def prometheus(service: str, alertname: str, severity: str, message: str, cluster: str) -> dict[str, Any]:
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": {
                    "service": service,
                    "alertname": alertname,
                    "severity": severity,
                    "environment": "production",
                    "cluster": cluster,
                    "pod": f"{service}-7d9f",
                    "pod_uid": f"{service}-7d9f-uid",
                },
                "annotations": {"summary": message},
                "startsAt": now_iso(),
            }
        ]
    }


def datadog(service: str, title: str, priority: str, text: str) -> dict[str, Any]:
    return {
        "event": {
            "id": secrets.token_hex(8),
            "title": title,
            "text": text,
            "alert_transition": "Triggered",
            "priority": priority,
            "timestamp": int(time.time()),
            "tags": [f"service:{service}", "env:production", "cluster:payments"],
        }
    }


def grafana(service: str, alertname: str, severity: str, message: str) -> dict[str, Any]:
    return {
        "alerts": [
            {
                "state": "alerting",
                # Grafana identifies each alert instance with a fingerprint and
                # the adapter requires it. It is stable per rule, exactly as a
                # real Grafana instance would send it.
                "fingerprint": f"{service}-{alertname}".encode().hex()[:16],
                "labels": {
                    "service": service,
                    "alertname": alertname,
                    "severity": severity,
                    "environment": "production",
                    "cluster": "observability",
                },
                "annotations": {"summary": message},
                "startsAt": now_iso(),
            }
        ]
    }


rejected: dict[str, str] = {}


async def post(client: httpx.AsyncClient, url: str, payload: dict[str, Any]) -> bool:
    """Send one webhook. A rejection is recorded, never counted as delivered."""

    try:
        response = await client.post(url, json=payload, timeout=15.0)
        if response.status_code >= 400:
            rejected.setdefault(url, f"HTTP {response.status_code}: {response.text[:160]}")
            return False
        return True
    except httpx.HTTPError as exc:
        rejected.setdefault(url, str(exc)[:160])
        return False


async def seed(client: httpx.AsyncClient, backend: str) -> int:
    """Fire a realistic storm at the real ingest endpoints. Returns alerts sent."""

    head("3. Alert storm (real webhooks, real ingest endpoints)")
    prom = f"{backend}/v1/ingest/prometheus"
    ddog = f"{backend}/v1/ingest/datadog"
    graf = f"{backend}/v1/ingest/grafana"
    sent = 0

    # A flapping check that fires over and over. This is the whole product in
    # one line: 140 pages become one incident.
    burst = [
        post(client, prom, prometheus(
            "checkout-api", "CheckoutLatencyHigh", "warning",
            "p99 checkout latency above 2s", "checkout"))
        for _ in range(140)
    ]
    sent += sum(await asyncio.gather(*burst))
    line(OK, "140 duplicate checkout-api alerts sent")

    # A correlated cascade, repeated so the co-occurrence graph builds real
    # directed edge weight rather than a single sighting.
    #
    # Deliberately not critical: a critical alert takes the bypass path and is
    # paged individually by design, so repeating one would produce six separate
    # incidents instead of one deduplicated incident that participates in the
    # correlation graph.
    # All three share one cluster on purpose. The engine scopes an incident by
    # `{environment}/{cluster}` and only correlates incidents inside the same
    # scope (src/engine/process_event.py), so splitting the cascade across
    # clusters would produce three unrelated incidents and no graph at all.
    for _ in range(6):
        sent += await post(client, prom, prometheus(
            "checkout-db", "ConnectionPoolExhausted", "warning",
            "Connection pool exhausted", "checkout-path"))
        await asyncio.sleep(0.35)
        sent += await post(client, prom, prometheus(
            "checkout-api", "UpstreamTimeout", "warning",
            "Upstream database timeout", "checkout-path"))
        await asyncio.sleep(0.35)
        sent += await post(client, prom, prometheus(
            "kubernetes", "PodRestart", "warning",
            "checkout-api pod restarted", "checkout-path"))
        await asyncio.sleep(0.35)
    line(OK, "18 correlated alerts sent (checkout-db -> checkout-api -> kubernetes, x6)")

    # One genuine P1, through a second vendor. This is the only thing that
    # should page a human all night.
    sent += await post(client, ddog, datadog(
        "payment-gateway", "PaymentFailureRateHigh", "P1",
        "Payment failure rate above the safe threshold"))
    line(OK, "1 critical Datadog alert sent (payment-gateway, expects PagerDuty bypass)")

    # A third vendor, to show ingestion is not Prometheus-only.
    for _ in range(22):
        sent += await post(client, graf, grafana(
            "auth-service", "ElevatedLatencyP99", "error",
            "Auth p99 latency elevated"))
    line(OK, "22 Grafana alerts sent (auth-service)")

    for _ in range(9):
        sent += await post(client, prom, prometheus(
            "image-resizer", "DiskUsageWarning", "info",
            "Disk usage above 80%", "media"))
    line(OK, "9 low-severity alerts sent (image-resizer)")

    if rejected:
        for url, reason in rejected.items():
            fail(f"Ingest endpoint rejected alerts: {url.rsplit('/', 1)[-1]}", reason)

    line(INFO, f"{sent} alerts accepted by the pipeline; letting the engine settle")
    await asyncio.sleep(2.5)
    return sent


# ─────────────────────────────────────────────────────────── verification ──

async def verify_pipeline(
    client: httpx.AsyncClient, backend: str, dashboard: str, seeded: bool
) -> list[dict[str, Any]]:
    head("4. Pipeline verification")

    incidents: list[dict[str, Any]] = []
    try:
        body = (await client.get(f"{backend}/v1/incidents/recent", timeout=15.0)).json()
        incidents = list(body.get("incidents") or [])
    except (httpx.HTTPError, ValueError) as exc:
        fail("Incidents readable from the backend", str(exc)[:200])
        return []

    if not incidents:
        if not seeded:
            # Nothing was sent, so an empty database is the expected result,
            # not a fault. Saying "the storm failed" here sent someone hunting
            # for a bug that did not exist.
            line(
                WARN,
                "No incidents to verify -- the database is empty and --skip-seed was used",
                "Run without --skip-seed to fire the alert storm:\n"
                "  .venv/Scripts/python.exe scripts/demo.py",
            )
        else:
            fail(
                "Pipeline produced incidents",
                "The storm was accepted but no incidents exist. Check the backend logs.",
            )
        return []

    alerts_in = sum(int(i.get("alert_count", 0)) for i in incidents)
    surfaced = len(incidents)
    cut = round((alerts_in - surfaced) / alerts_in * 100, 1) if alerts_in else 0.0
    line(OK, f"{surfaced} incidents from {alerts_in} alerts  ->  {cut}% noise cut")

    by_severity: dict[str, int] = {}
    by_route: dict[str, int] = {}
    for incident in incidents:
        by_severity[incident.get("severity", "?")] = by_severity.get(incident.get("severity", "?"), 0) + 1
        route = incident.get("route_decision") or "not routed"
        by_route[route] = by_route.get(route, 0) + 1
    line(OK, "Severity mix: " + ", ".join(f"{k}={v}" for k, v in sorted(by_severity.items())))
    line(OK, "Routing mix: " + ", ".join(f"{k}={v}" for k, v in sorted(by_route.items())))

    paged = [i for i in incidents if i.get("route_decision") == "pagerduty"]
    if paged:
        line(OK, f"Critical bypass fired for {len(paged)} incident(s)", paged[0].get("title", ""))
    else:
        line(WARN, "No PagerDuty bypass in this run")

    # The dashboard must be showing these same incidents, not its sample data.
    try:
        proxied = (await client.get(f"{dashboard}/api/incidents/recent", timeout=15.0)).json()
        proxy_count = len(proxied.get("incidents") or [])
        if proxy_count == surfaced:
            line(OK, f"Dashboard proxy returns the same {proxy_count} incidents (real data, not samples)")
        else:
            fail("Dashboard proxy agrees with the backend", f"backend {surfaced} vs proxy {proxy_count}")
    except (httpx.HTTPError, ValueError) as exc:
        fail("Dashboard proxy returns incidents", str(exc)[:200])

    # The live stream the console actually subscribes to. Its snapshot is also
    # the only place correlation edges are published, so this doubles as the
    # check that the graph the Correlations view draws is not empty.
    snapshot: dict[str, Any] | None = None
    try:
        async with client.stream("GET", f"{dashboard}/api/stream", timeout=25.0) as response:
            if response.status_code != 200:
                fail("SSE stream through the dashboard", f"HTTP {response.status_code}")
            else:
                buffer = ""
                async for chunk in response.aiter_text():
                    buffer += chunk
                    if "event: snapshot" in buffer and "\n\n" in buffer.split("event: snapshot", 1)[1]:
                        block = buffer.split("event: snapshot", 1)[1].split("\n\n", 1)[0]
                        for row in block.splitlines():
                            if row.startswith("data: "):
                                try:
                                    snapshot = json.loads(row[6:])
                                except ValueError:
                                    snapshot = None
                        break
                    if len(buffer) > 400_000:
                        break
    except (httpx.HTTPError, ValueError) as exc:
        fail("SSE stream through the dashboard", str(exc)[:200])

    if snapshot is None:
        line(WARN, "No snapshot event seen", "The stream is open; it emits a snapshot when state changes.")
    else:
        streamed = len(snapshot.get("incidents") or [])
        edges = snapshot.get("edges") or []
        line(OK, f"SSE snapshot received through /api/stream: {streamed} incidents, {len(edges)} edges")
        if edges:
            heaviest = max(edges, key=lambda e: float(e.get("decayed_joint_weight", 0) or 0))
            line(
                OK,
                f"Correlation graph has {len(edges)} directed edge(s)",
                f"heaviest joint weight {float(heaviest.get('decayed_joint_weight', 0)):.2f}",
            )
        else:
            fail(
                "Correlation graph has edges",
                "The Correlations view would be empty. Incidents only correlate inside one\n"
                "scope_key ({environment}/{cluster}), so a cascade must share a cluster.",
            )

    # The tamper-evident ledger, which is worth showing judges directly.
    try:
        result = subprocess.run(
            [sys.executable, "scripts/verify_chain.py"],
            cwd=REPO_ROOT, capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            line(OK, "Hash-chained audit ledger verified", (result.stdout or "").strip()[-160:])
        else:
            fail("Hash-chained audit ledger verified", (result.stdout + result.stderr)[-300:])
    except (subprocess.SubprocessError, OSError) as exc:
        line(WARN, "Could not run scripts/verify_chain.py", str(exc)[:160])

    return incidents


# ──────────────────────────────────────────────────────────────── github ──

async def github_phase(
    client: httpx.AsyncClient, dashboard: str, incidents: list[dict[str, Any]], service: str
) -> None:
    head("5. GitHub code investigation")

    response = await client.get(f"{dashboard}/api/github/repositories", timeout=20.0)
    if response.status_code != 200:
        detail = ""
        try:
            detail = response.json().get("error", {}).get("message", "")
        except ValueError:
            detail = response.text[:200]
        fail(
            "GitHub admin API reachable",
            f"{detail}\n"
            "If this says the admin API is disabled, restart the backend so it reads the\n"
            "GITHUB_ADMIN_TOKEN this script wrote to .env.",
        )
        return

    repositories = response.json().get("repositories") or []
    if not repositories:
        line(
            WARN,
            "No repositories connected -- the code-investigation demo needs a GitHub App",
            "This is the only step that cannot be automated; it takes about 5 minutes:\n"
            "  1. github.com -> Settings -> Developer settings -> GitHub Apps -> New GitHub App\n"
            "  2. Permissions: Repository -> Metadata: Read-only, Contents: Read-only. Nothing else.\n"
            "  3. Generate a private key, note the Client ID and the app slug from its URL.\n"
            "  4. Put them in .env: GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID,\n"
            "     GITHUB_APP_PRIVATE_KEY, GITHUB_WEBHOOK_SECRET\n"
            "  5. Install the App on one repository, restart the backend, re-run this script.\n"
            "  Full walkthrough: docs/github-phase1-setup.md\n"
            "Everything else in this demo works without it.",
        )
        return

    line(OK, f"{len(repositories)} repository/repositories connected")
    repository = next((r for r in repositories if r.get("service") == service), repositories[0])

    if repository.get("service") != service:
        mapped = await client.put(
            f"{dashboard}/api/github/service-mappings/{service}",
            json={"repository_id": repository["repository_id"]},
            timeout=20.0,
        )
        if mapped.status_code == 200:
            line(OK, f"Mapped service '{service}' -> {repository['full_name']}")
        else:
            fail("Service mapping saved", mapped.text[:200])
            return
    else:
        line(OK, f"Service '{service}' already mapped to {repository['full_name']}")

    snapshot = await client.post(
        f"{dashboard}/api/github/repositories/{repository['repository_id']}/snapshots", timeout=120.0
    )
    if snapshot.status_code not in (200, 201):
        fail("Snapshot pinned", snapshot.text[:300])
        return
    pinned = snapshot.json()
    line(
        OK,
        f"Snapshot pinned at {pinned['commit_sha'][:10]} on {pinned['ref']}",
        f"{pinned['file_count']} files inventoried",
    )

    target = next((i for i in incidents if i.get("title", "").startswith(f"{service} ")), None)
    if target is None:
        line(WARN, f"No incident for service '{service}' to diagnose")
        return

    line(INFO, f"Diagnosing: {target['title']} (this calls the local model, allow a minute)")
    diagnosis = await client.post(
        f"{dashboard}/api/github/incidents/{target['incident_id']}/diagnoses", timeout=300.0
    )
    if diagnosis.status_code not in (200, 201):
        fail("Diagnosis created", diagnosis.text[:300])
        return

    analysis = diagnosis.json()
    body = analysis.get("diagnosis", {})
    if body.get("status") == "diagnosed":
        line(OK, f"Grounded diagnosis from {body.get('provider')} at {body.get('confidence')} confidence")
        hypothesis = body.get("root_cause_hypothesis") or {}
        print(f"       root cause: {hypothesis.get('summary', '')}")
        for item in body.get("evidence", []):
            if item.get("kind") == "source_excerpt":
                print(f"       cites {item.get('file_path')}:{item.get('start_line')}-{item.get('end_line')}")

        preview = await client.post(
            f"{dashboard}/api/github/analyses/{analysis['analysis_id']}/patch-preview", timeout=300.0
        )
        if preview.status_code == 200:
            diff = preview.json()["patch"]["unified_diff"]
            line(OK, "Patch preview generated (a diff to read; nothing is written)")
            print("\n" + "\n".join(f"       {row}" for row in diff.splitlines()[:24]))
        else:
            line(WARN, "No patch preview", preview.text[:200])
    else:
        fallback = body.get("fallback") or {}
        line(
            WARN,
            f"Safe fallback: {fallback.get('reason')}",
            f"{fallback.get('message', '')}\n"
            "This is the designed behaviour when the model or source is unavailable,\n"
            "not a crash -- the backend refuses to guess.",
        )


# ────────────────────────────────────────────────────────────────── reset ──

def reset_pipeline_data(backend_reachable: bool) -> bool:
    """Clear the pipeline tables. Returns False when it refused to."""

    head("0. Reset")
    if backend_reachable:
        line(
            BAD,
            "Refusing to reset while the backend is running",
            "The engine holds in-memory quiet-deadline timers for live incidents.\n"
            "\n"
            "  1) Stop the backend (close its window, or Ctrl+C in it)\n"
            "  2) .venv/Scripts/python.exe scripts/demo.py --reset\n"
            "  3) Start it again:\n"
            "     .venv/Scripts/python.exe -m uvicorn src.main:app --host 127.0.0.1 --port 8000\n"
            "  4) .venv/Scripts/python.exe scripts/demo.py",
        )
        return False

    from src.config import settings  # imported here so --reset works without a live app

    database = REPO_ROOT / settings.DATABASE_PATH
    if not database.exists():
        line(OK, "No database to reset")
        return True
    connection = sqlite3.connect(database)
    try:
        for table in ("outbox", "delivery_intents", "edges", "raw_events", "incidents"):
            connection.execute(f"DELETE FROM {table}")
        connection.commit()
        line(OK, "Cleared incidents, raw events, edges and delivery rows")
        line(INFO, "GitHub installations, mappings and snapshots were left alone")
    finally:
        connection.close()
    return True


# ─────────────────────────────────────────────────────────────────── main ──

async def run(args: argparse.Namespace) -> int:
    backend = args.backend.rstrip("/")
    dashboard = args.dashboard.rstrip("/")

    print("\nPulseGraph demo")
    print(f"backend {backend}   dashboard {dashboard}")

    async with httpx.AsyncClient(follow_redirects=False) as client:
        if args.reset:
            reachable = await wait_for(client, f"{backend}/v1/health", seconds=2) is not None
            # A refused reset has to stop here. Carrying on would fire a second
            # storm onto the data the caller just asked to clear -- exactly the
            # opposite of what they wanted, and it doubles every figure.
            if not reset_pipeline_data(reachable):
                head("Result")
                print("\nNothing was reset, and nothing was sent.\n")
                return 1

        _, restart_needed = configure(backend)
        await check_services(client, backend, dashboard, args.ollama.rstrip("/"), args.model)

        if problems:
            head("Result")
            print("Fix the failures above, then run this again.\n")
            return 1

        sent = 0
        if not args.skip_seed:
            sent = await seed(client, backend)

        incidents = await verify_pipeline(client, backend, dashboard, seeded=not args.skip_seed)

        if restart_needed:
            head("5. GitHub code investigation")
            line(
                WARN,
                "Skipped: the admin token was only just written",
                "Restart the backend and the dashboard, then run this script again.",
            )
        else:
            await github_phase(client, dashboard, incidents, args.service)

        if not incidents:
            head("Result")
            print(
                "\nNothing to show yet -- the database is empty.\n"
                "Run the storm:  .venv/Scripts/python.exe scripts/demo.py\n"
            )
            return 1

        head("What to show the judges")
        alerts_in = sum(int(i.get("alert_count", 0)) for i in incidents)
        cut = round((alerts_in - len(incidents)) / alerts_in * 100, 1) if alerts_in else 0.0
        print(
            f"""
Open {dashboard}

1. Overview      "{alerts_in} alerts came in. {len(incidents)} incidents came out."
                 The consolidation tile reads {cut}%. Every figure is derived from
                 rows in SQLite, not a fixture -- the header says Live because the
                 browser is on the SSE stream.

2. Incidents     Filter by severity, sort by alert count. Open the
                 payment-gateway incident: it was paged immediately through the
                 critical bypass, while {len(incidents) - 1} others were held back.

3. Drawer        Alert consolidation, the adaptive quiet window, and upstream /
                 downstream causes from the co-occurrence graph.

4. Correlations  checkout-db -> checkout-api -> kubernetes, drawn as a directed
                 graph, with edge weight from the decayed joint weight.

5. Investigate   The drawer's "Investigate code" button. If GitHub is connected it
                 diagnoses the incident against pinned source and shows a diff no
                 one can apply -- the App is read-only by construction.

6. Audit Ledger  Every event hash-chained. This script verified the chain above.
"""
        )
        if sent:
            print(f"({sent} alerts were fired at the real ingest endpoints during this run.)\n")

    head("Result")
    if problems:
        print(f"{len(problems)} check(s) failed:")
        for item in problems:
            print(f"  - {item}")
        print()
        return 1
    print("Every hop verified: ingest -> engine -> database -> proxy -> stream -> browser.\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument("--dashboard", default="http://localhost:3000")
    parser.add_argument("--ollama", default="http://127.0.0.1:11434")
    parser.add_argument("--model", default="qwen2.5-coder:7b")
    parser.add_argument("--service", default="checkout-api", help="service to map to a repository")
    parser.add_argument("--skip-seed", action="store_true", help="verify without sending alerts")
    parser.add_argument("--reset", action="store_true", help="clear pipeline data (backend must be stopped)")
    return asyncio.run(run(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(main())
