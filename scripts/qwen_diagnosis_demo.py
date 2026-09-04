"""Prove the local-model half of the incident-to-patch workflow, without GitHub.

Feeds a deliberately broken source file and a matching incident straight into
the same `OllamaLocalProvider` the backend uses, then runs the proposal through
the same patch workspace, and prints what Qwen concluded and what it wants
changed.

This exists because a real diagnosis needs a registered GitHub App and a pinned
snapshot, which take external setup. The model half does not, so it can be
verified on its own the moment Ollama is up. What it does NOT cover: the GitHub
read path, the snapshot pin, and persistence. Use
`scripts/check_github_integration.py` for those.

Nothing is written anywhere. The patch workspace is a temporary directory that
is deleted before this returns, exactly as it is in the API.

    python scripts/qwen_diagnosis_demo.py
    python scripts/qwen_diagnosis_demo.py --timeout 300
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import sys
from uuid import uuid4

sys.path.insert(0, ".")

from src.config import settings  # noqa: E402
from src.github_integration.diagnosis import (  # noqa: E402
    DiagnosisIncidentContext,
    DiagnosisRequest,
    DiagnosisResult,
    SourceExcerpt,
    SourceSnapshotReference,
)
from src.github_integration.ollama_provider import (  # noqa: E402
    OllamaLocalError,
    OllamaLocalLimits,
    OllamaLocalProvider,
    PatchSourceFile,
)
from src.github_integration.workflow import bind_patch_to_snapshot  # noqa: E402
from src.github_integration.workspace import LocalPatchWorkspace, PatchWorkspaceError  # noqa: E402

# A small, self-contained bug: the discount rate divides by the basket size
# without guarding the empty basket, so an empty cart raises ZeroDivisionError
# and takes the pricing endpoint down. Realistic, and unambiguous enough that a
# failure to spot it means the model or the wiring is wrong, not the puzzle.
BUGGY_SOURCE = '''"""Basket pricing for the checkout service."""

from decimal import Decimal


def average_unit_price(items: list[dict]) -> Decimal:
    """Mean price per unit across the basket."""

    total = sum(Decimal(str(item["price"])) for item in items)
    return total / len(items)


def basket_discount(items: list[dict], tier: str) -> Decimal:
    """Discount rate for a basket, scaled by the average unit price."""

    average = average_unit_price(items)
    if tier == "gold":
        return average * Decimal("0.15")
    if tier == "silver":
        return average * Decimal("0.10")
    return Decimal("0")


def price_basket(items: list[dict], tier: str) -> Decimal:
    subtotal = sum(Decimal(str(item["price"])) * item["quantity"] for item in items)
    return subtotal - basket_discount(items, tier)
'''

SOURCE_PATH = "src/checkout/pricing.py"

# OllamaLocalProvider._validate_timeout refuses anything above this.
MAX_TIMEOUT_SECONDS = 120.0


def git_blob_sha(text: str) -> str:
    """The SHA-1 Git would store this content under, so the ID is a real one."""

    data = text.encode("utf-8")
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def build_request() -> DiagnosisRequest:
    blob_sha = git_blob_sha(BUGGY_SOURCE)
    incident = DiagnosisIncidentContext(
        incident_id=uuid4(),
        service="checkout-api",
        alertname="UnhandledException",
        severity="critical",
        status="firing",
        scope_key="prod/eu-west-1",
        alert_count=63,
        message=(
            "ZeroDivisionError: division by zero in average_unit_price "
            "(src/checkout/pricing.py). 63 occurrences in 4 minutes; every "
            "request to POST /basket/price with an empty basket returns 500."
        ),
        labels={"environment": "prod", "endpoint": "/basket/price"},
        summary="Pricing endpoint returning 500 for empty baskets.",
        graph_root_cause_hint="Onset correlates with the checkout-api deploy at 14:02.",
    )
    snapshot = SourceSnapshotReference(
        snapshot_id=uuid4(),
        repository_id=1,
        repository_full_name="pulsegraph/checkout-api",
        commit_sha=git_blob_sha("demo-commit"),
        tree_sha=git_blob_sha("demo-tree"),
    )
    excerpt = SourceExcerpt(
        file_path=SOURCE_PATH,
        blob_sha=blob_sha,
        start_line=1,
        end_line=BUGGY_SOURCE.count("\n") + 1,
        content=BUGGY_SOURCE,
        language="python",
    )
    return DiagnosisRequest(incident=incident, snapshot=snapshot, excerpts=[excerpt])


def print_diagnosis(result: DiagnosisResult) -> None:
    print(f"\nstatus     {result.status}")
    print(f"provider   {result.provider}")
    print(f"confidence {result.confidence:.2f}")

    if result.status == "fallback" and result.fallback:
        print("\nNo grounded diagnosis was produced. The backend returns this instead of")
        print("guessing, which is the designed behaviour, not a crash.")
        print(f"\n  reason  {result.fallback.reason}")
        print(f"  {result.fallback.message}")
        for step in result.fallback.next_steps:
            print(f"    - {step}")
        return

    if result.root_cause_hypothesis:
        print("\nROOT CAUSE")
        print(f"  {result.root_cause_hypothesis.summary}")
        print(f"\n  {result.root_cause_hypothesis.reasoning}")

    if result.evidence:
        print("\nEVIDENCE")
        for item in result.evidence:
            if item.kind == "source_excerpt":
                print(f"  {item.file_path}:{item.start_line}-{item.end_line}")
                print(f"    {item.explanation}")
            else:
                print(f"  (incident) {item.explanation}")

    if result.proposed_fix:
        print("\nPROPOSED FIX")
        print(f"  {result.proposed_fix.summary}")
        for index, step in enumerate(result.proposed_fix.steps, start=1):
            print(f"    {index}. {step}")
        if result.proposed_fix.affected_paths:
            print(f"  files: {', '.join(result.proposed_fix.affected_paths)}")
        print("  requires human review: yes; automatically applied: no")


async def run(timeout: float) -> int:
    if not settings.OLLAMA_ENABLED:
        print("OLLAMA_ENABLED is false in .env. Set it to true and re-run.")
        return 1

    request = build_request()
    print("=" * 70)
    print("Incident   checkout-api - UnhandledException (63 alerts)")
    print(f"Source     {SOURCE_PATH}  ({len(BUGGY_SOURCE)} bytes, 1 excerpt)")
    print(f"Model      {settings.OLLAMA_MODEL} at {settings.OLLAMA_BASE_URL}")
    print("=" * 70)
    print("\nAsking the model... (a 7B model on CPU can take a minute or two)")

    limits = OllamaLocalLimits(max_output_tokens=settings.OLLAMA_MAX_OUTPUT_TOKENS)
    async with OllamaLocalProvider(
        settings.OLLAMA_MODEL,
        base_url=settings.OLLAMA_BASE_URL,
        timeout=timeout,
        limits=limits,
    ) as provider:
        try:
            result = await provider.diagnose(request)
        except OllamaLocalError as exc:
            print(f"\nThe model call failed: {type(exc).__name__}: {exc}")
            print("\nChecks worth making, in order:")
            print(f"  curl {settings.OLLAMA_BASE_URL}/api/version")
            print(f"  ollama list        (does it show {settings.OLLAMA_MODEL}?)")
            print("  HTTP 404 usually means OLLAMA_MODEL does not match a pulled model")
            print("  HTTP 400 usually means this Ollama build rejected the request shape")
            return 1

        print_diagnosis(result)

        if result.status != "diagnosed" or result.proposed_fix is None:
            print("\nNo patch preview: that needs a grounded diagnosis.")
            return 0

        print("\n" + "-" * 70)
        print("Asking the model for a reviewable patch...")

        base_files = {SOURCE_PATH: BUGGY_SOURCE}
        source_files = [
            PatchSourceFile(
                path=SOURCE_PATH,
                blob_sha=git_blob_sha(BUGGY_SOURCE),
                content=BUGGY_SOURCE,
            )
        ]
        try:
            proposal = await provider.propose_patch(
                request, result, source_files, patch_id=f"demo-{uuid4().hex[:12]}"
            )
            scoped = bind_patch_to_snapshot(
                proposal,
                base_files=base_files,
                allowed_paths=tuple(result.proposed_fix.affected_paths) or (SOURCE_PATH,),
            )
            with LocalPatchWorkspace(base_files) as workspace:
                review = workspace.apply(scoped)
        except (OllamaLocalError, PatchWorkspaceError, ValueError) as exc:
            print(f"\nNo patch preview: {type(exc).__name__}: {exc}")
            print("The diagnosis above is still valid; only the patch step failed.")
            return 0

        print(f"\n{review.summary}")
        if review.rationale:
            print(f"{review.rationale}")
        for change in review.changed_files:
            print(f"\n  {change.action.value} {change.path}")
            if change.explanation:
                print(f"    {change.explanation}")

        print("\nUNIFIED DIFF (preview only - nothing was written)\n")
        print(review.unified_diff)

    print("-" * 70)
    print("The workspace above was a temporary directory and is already deleted.")
    print("PulseGraph cannot push, commit, branch, merge, or open a pull request.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--timeout",
        type=float,
        default=MAX_TIMEOUT_SECONDS,
        help=(
            "Seconds to allow per model call, capped at "
            f"{MAX_TIMEOUT_SECONDS:.0f} by the provider. The API default of "
            f"{settings.OLLAMA_TIMEOUT_SECONDS}s is often too short for a 7B model on CPU."
        ),
    )
    args = parser.parse_args()
    # The provider rejects anything above its own ceiling, so clamp rather than
    # letting a generous --timeout fail before the first request.
    return asyncio.run(run(min(args.timeout, MAX_TIMEOUT_SECONDS)))


if __name__ == "__main__":
    raise SystemExit(main())
