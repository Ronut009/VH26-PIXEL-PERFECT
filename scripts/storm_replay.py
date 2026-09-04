"""Deterministic end-to-end PulseGraph demo traffic generator."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import sys
from typing import Any

import httpx

DEFAULT_INGEST_URL = "http://127.0.0.1:8000/v1/ingest/prometheus"
GRAPH_STEP_DELAY_SECONDS = 0.5

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
        "+00:00", "Z"
    )


def _prometheus_payload(
    *,
    service: str,
    alertname: str,
    severity: str,
    message: str,
    environment: str = "demo",
    cluster: str = "pulsegraph-storm",
) -> dict[str, list[dict[str, Any]]]:
    labels = {
        "service": service,
        "alertname": alertname,
        "severity": severity,
        "environment": environment,
        "cluster": cluster,
        "pod": f"{service}-demo-pod",
        "pod_uid": f"{service}-demo-pod-uid",
    }
    return {
        "alerts": [
            {
                "status": "firing",
                "labels": labels,
                "annotations": {"summary": message},
                "startsAt": _timestamp(),
            }
        ]
    }


async def _send(
    client: httpx.AsyncClient, ingest_url: str, payload: dict[str, Any]
) -> None:
    response = await client.post(ingest_url, json=payload)
    response.raise_for_status()


async def replay(*, ingest_url: str, inter_phase_delay: float) -> None:
    timeout = httpx.Timeout(10.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        duplicate_payload = _prometheus_payload(
            service="checkout-api",
            alertname="CheckoutLatencyHigh",
            severity="warning",
            message="Checkout latency is elevated",
        )
        await asyncio.gather(
            *(_send(client, ingest_url, duplicate_payload) for _ in range(10))
        )
        print("Phase 1 Complete: 10 duplicates sent → expect 1 incident")

        await asyncio.sleep(inter_phase_delay)

        await _send(
            client, ingest_url,
            _prometheus_payload(
                service="database",
                alertname="DatabaseFailure",
                severity="warning",
                message="Database connection pool failure",
                environment="demo-graph",
                cluster="correlation-storm",
            ),
        )
        await asyncio.sleep(GRAPH_STEP_DELAY_SECONDS)
        await _send(
            client, ingest_url,
            _prometheus_payload(
                service="api",
                alertname="ApiTimeout",
                severity="warning",
                message="API timeout burst detected",
                environment="demo-graph",
                cluster="correlation-storm",
            ),
        )
        await asyncio.sleep(GRAPH_STEP_DELAY_SECONDS)
        await _send(
            client, ingest_url,
            _prometheus_payload(
                service="kubernetes",
                alertname="PodRestart",
                severity="warning",
                message="Pod restart burst detected",
                environment="demo-graph",
                cluster="correlation-storm",
            ),
        )
        print("Phase 2 Complete: DB→API→Pod burst sent → expect directed edge + root cause=DB")

        await asyncio.sleep(inter_phase_delay)

        await _send(
            client, ingest_url,
            _prometheus_payload(
                service="payment-gateway",
                alertname="PaymentFailure",
                severity="critical",
                message="Payment failure rate exceeded safe threshold",
            ),
        )
        print(
            "Phase 3 Complete: Critical payment failure sent → "
            "expect immediate PagerDuty intent + bypass audit"
        )


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="seconds to wait between demo phases (default: 1.0)",
    )
    parser.add_argument(
        "--ingest-url",
        default=DEFAULT_INGEST_URL,
        help=f"Prometheus ingest endpoint (default: {DEFAULT_INGEST_URL})",
    )
    arguments = parser.parse_args()
    if arguments.delay < 0:
        parser.error("--delay must be non-negative")
    return arguments


def main() -> int:
    arguments = _arguments()
    try:
        asyncio.run(replay(ingest_url=arguments.ingest_url, inter_phase_delay=arguments.delay))
    except (httpx.HTTPError, OSError) as exc:
        print(f"Storm replay failed: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
