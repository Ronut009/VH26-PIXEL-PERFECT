"""Preflight for the GitHub incident-to-patch feature.

Walks the integration in the order it actually has to work, and for every step
that fails prints the specific thing to change. Read-only: it makes GET
requests to the backend and to Ollama, and writes nothing anywhere.

    python scripts/check_github_integration.py
    python scripts/check_github_integration.py --dashboard http://localhost:3000
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

sys.path.insert(0, ".")

from src.config import settings  # noqa: E402

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"
WARN = "WARN"

_SYMBOL = {PASS: "[ ok ]", FAIL: "[fail]", SKIP: "[skip]", WARN: "[warn]"}

failures = 0
skipped = 0


def report(status: str, title: str, detail: str = "", fix: str = "") -> None:
    global failures, skipped
    if status == FAIL:
        failures += 1
    if status == SKIP:
        skipped += 1
    print(f"{_SYMBOL[status]} {title}")
    if detail:
        print(f"        {detail}")
    if fix:
        for line in fix.splitlines():
            print(f"        -> {line}")


def get(url: str, token: str | None = None, timeout: float = 10.0) -> tuple[int, Any]:
    """Return (status, parsed body). Status 0 means the host never answered."""

    request = Request(url, method="GET")
    request.add_header("accept", "application/json")
    if token:
        request.add_header("authorization", f"Bearer {token}")
    try:
        with urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
            try:
                return response.status, json.loads(raw)
            except json.JSONDecodeError:
                return response.status, raw
    except HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, raw
    except (URLError, OSError, TimeoutError):
        return 0, None


def detail_of(body: Any) -> str:
    if isinstance(body, dict):
        if isinstance(body.get("detail"), str):
            return body["detail"]
        error = body.get("error")
        if isinstance(error, dict) and isinstance(error.get("message"), str):
            return error["message"]
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--dashboard",
        default="http://localhost:3000",
        help="Next.js origin; its proxy is checked too when it is running.",
    )
    args = parser.parse_args()
    backend = args.backend.rstrip("/")
    dashboard = args.dashboard.rstrip("/")

    print(f"\nPulseGraph GitHub integration preflight\nbackend {backend}\n" + "-" * 62)

    # 1 ─ backend reachable and its writer connection usable
    status, body = get(f"{backend}/v1/health")
    if status == 0:
        report(
            FAIL,
            "Backend reachable",
            f"Nothing answered on {backend}.",
            "Start it: uvicorn src.main:app --reload",
        )
        print("\nEverything else depends on the backend. Stopping here.\n")
        return 1
    if isinstance(body, dict) and body.get("status") == "healthy":
        report(PASS, "Backend reachable and database healthy")
    else:
        report(
            FAIL,
            "Backend database healthy",
            f"/v1/health said: {body}",
            "Check DATABASE_PATH, then: python scripts/init_db.py",
        )

    # 2 ─ the admin token, which gates every GitHub route
    token = settings.GITHUB_ADMIN_TOKEN
    if not token:
        report(
            FAIL,
            "GITHUB_ADMIN_TOKEN set in .env",
            "Empty, so the backend disables every /v1/github/* route.",
            "Pick any long random string, then put the SAME value in both:\n"
            "  .env               GITHUB_ADMIN_TOKEN=<value>\n"
            "  web/.env.local     GITHUB_ADMIN_TOKEN=<value>\n"
            "Restart the backend and the dashboard afterwards.",
        )
    else:
        report(PASS, "GITHUB_ADMIN_TOKEN set in .env")

    status, body = get(f"{backend}/v1/github/repositories", token=token)
    repositories: list[dict[str, Any]] = []
    if status == 503:
        report(
            FAIL,
            "GitHub admin API enabled",
            detail_of(body),
            "The running backend has no GITHUB_ADMIN_TOKEN. Set it in .env and restart -\n"
            "a running process does not pick up .env changes.",
        )
    elif status == 401:
        report(
            FAIL,
            "GitHub admin API accepts this token",
            "The backend rejected the token in .env.",
            "The running backend was started with a different value. Restart it.",
        )
    elif status == 200 and isinstance(body, dict):
        repositories = list(body.get("repositories") or [])
        report(PASS, "GitHub admin API reachable", f"{len(repositories)} repositories connected")
    else:
        report(FAIL, "GitHub admin API reachable", f"HTTP {status}: {detail_of(body) or body}")

    # 3 ─ the dashboard proxy, which is what the browser actually calls
    status, body = get(f"{dashboard}/api/health")
    if status == 0:
        report(
            SKIP,
            "Dashboard proxy",
            f"Nothing answered on {dashboard}.",
            "Optional. To include it: cd web && npm run dev",
        )
    else:
        status, body = get(f"{dashboard}/api/github/repositories")
        if status == 200:
            report(PASS, "Dashboard proxy reaches the GitHub API")
        elif status == 503 and isinstance(body, dict) and detail_of(body).startswith("GITHUB_ADMIN_TOKEN is not set"):
            report(
                FAIL,
                "Dashboard proxy reaches the GitHub API",
                "web/.env.local has no GITHUB_ADMIN_TOKEN.",
                "Add it to web/.env.local (no NEXT_PUBLIC_ prefix) and restart the dashboard.",
            )
        else:
            report(
                FAIL,
                "Dashboard proxy reaches the GitHub API",
                f"HTTP {status}: {detail_of(body) or body}",
                "The token must match the backend's .env exactly.",
            )

    # 4 ─ the GitHub App itself
    status, body = get(f"{backend}/v1/github/install-url", token=token)
    if status == 200 and isinstance(body, dict):
        report(PASS, "GitHub App registered", body.get("install_url", ""))
    elif status == 503:
        report(
            FAIL,
            "GitHub App registered",
            detail_of(body),
            "Register a read-only GitHub App and fill GITHUB_APP_SLUG, GITHUB_APP_CLIENT_ID,\n"
            "GITHUB_APP_PRIVATE_KEY and GITHUB_WEBHOOK_SECRET in .env.\n"
            "Full walkthrough: docs/github-phase1-setup.md",
        )
    else:
        report(SKIP, "GitHub App registered", f"HTTP {status}")

    # 5 ─ repositories, mappings and snapshots
    if repositories:
        mapped = [r for r in repositories if r.get("service")]
        report(
            PASS if mapped else FAIL,
            "A service is mapped to a repository",
            f"{len(mapped)} of {len(repositories)} repositories have a service mapping.",
            ""
            if mapped
            else "Open the dashboard -> Code Investigation -> Repositories -> Map service.\n"
            "The name must match the service on incident titles exactly.",
        )
        for repository in mapped:
            report(
                PASS,
                f"  {repository['full_name']} <- {repository['service']}",
            )
    else:
        report(
            FAIL,
            "Repositories connected",
            "No repositories are selected on any installation.",
            "Install the App on the repositories you want readable:\n"
            "  dashboard -> Code Investigation -> Repositories -> Get install link",
        )

    # 6 ─ incidents to diagnose
    status, body = get(f"{backend}/v1/incidents/recent")
    incidents = list(body.get("incidents") or []) if isinstance(body, dict) else []
    if incidents:
        services = sorted({i.get("title", "").split(" — ")[0] for i in incidents})
        report(PASS, "Incidents available", f"{len(incidents)} incidents; services: {', '.join(services)}")
    else:
        report(
            FAIL,
            "Incidents available",
            "The database has no incidents, so there is nothing to diagnose.",
            "Generate some: python scripts/storm_replay.py --delay 1",
        )

    # 7 ─ the local model
    status, body = get(f"{settings.OLLAMA_BASE_URL.rstrip('/')}/api/tags", timeout=5)
    if not settings.OLLAMA_ENABLED:
        report(
            WARN,
            "Local model enabled",
            "OLLAMA_ENABLED is false, so diagnosis returns its safe fallback rather than a real analysis.",
            "Set OLLAMA_ENABLED=true in .env and restart the backend.",
        )
    elif status == 0:
        report(
            FAIL,
            "Ollama reachable",
            f"Nothing answered on {settings.OLLAMA_BASE_URL}.",
            "Start Ollama, then: ollama pull " + (settings.OLLAMA_MODEL or "qwen2.5-coder:7b"),
        )
    elif isinstance(body, dict):
        names = {m.get("name") for m in body.get("models", [])}
        if settings.OLLAMA_MODEL in names:
            report(PASS, "Ollama reachable", f"model {settings.OLLAMA_MODEL} is pulled")
        else:
            report(
                FAIL,
                "Configured model is pulled",
                f"{settings.OLLAMA_MODEL} not among: {', '.join(sorted(n for n in names if n))}",
                f"ollama pull {settings.OLLAMA_MODEL}",
            )

    print("-" * 62)
    if failures:
        print(
            f"\n{failures} step(s) need attention. Work down the list in order - each one\n"
            "unblocks the next. Once they all pass, the whole workflow is available in\n"
            "the dashboard under Code Investigation.\n"
        )
        return 1
    print(
        "\nEverything the GitHub workflow needs is in place. In the dashboard:\n"
        "  Incidents -> open one -> Investigate code -> Run diagnosis -> Generate patch preview\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
