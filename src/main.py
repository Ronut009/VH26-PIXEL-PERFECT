from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi import FastAPI, Header, HTTPException, Request


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.contracts import NormalizedEvent
from src.db.connection import Database, get_reader_connection
from src.db.writer import DbWriter
from src.engine.silence_sweeper import SilenceSweeper
from src.engine.timer_wheel import TimerWheel
from src.engine.timer_worker import TimerWorker
from src.github_integration.anthropic_provider import (
    AnthropicConfigurationError,
    AnthropicDiagnosisProvider,
)
from src.github_integration.client import GitHubConfigurationError, GitHubReadOnlyClient
from src.github_integration.diagnosis import DiagnosisService
from src.github_integration.ollama_provider import (
    OllamaLocalConfigurationError,
    OllamaLocalLimits,
    OllamaLocalProvider,
)
from src.github_integration.router import create_github_router
from src.graph.root_cause_worker import RootCauseWorker
from src.selfcheck.health import SelfCheckThresholds, evaluate
from src.selfcheck.heartbeat import HeartbeatEmitter
from src.selfcheck.signals import gather
from src.inbound.router import create_inbound_router
from src.engine.process_event import scope_key_for
from src.ingest.auth import (
    IngestAuthError,
    IngestNotConfigured,
    IngestScopeError,
    authenticate,
    authorize_scope,
    parse_tokens,
)
from src.ingest.normalize_datadog import normalize_datadog
from src.ingest.normalize_grafana import normalize_grafana
from src.ingest.prometheus import normalize_prometheus
from src.outbox.channel_health import BreakerConfig
from src.outbox.worker import OutboxWorker
from src.stream.sse_broker import create_sse_router
from src.utils.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.DATABASE_PATH)
    await db.connect()

    github_client: GitHubReadOnlyClient | None = None
    app.state.github_client = None
    if settings.github_app_is_configured:
        try:
            github_client = GitHubReadOnlyClient(
                settings.github_app_issuer,
                settings.GITHUB_APP_PRIVATE_KEY.replace("\\n", "\n"),
                timeout=settings.GITHUB_REQUEST_TIMEOUT_SECONDS,
                api_version=settings.GITHUB_API_VERSION,
            )
            app.state.github_client = github_client
        except GitHubConfigurationError as exc:
            # GitHub is optional; a bad optional configuration must not stop
            # the established alert-ingest pipeline from starting.
            logger.warning("github_integration_disabled", reason=str(exc))
    else:
        # Say so. Unconfigured used to be completely silent, so the only
        # symptom was an empty Code Investigation panel with no way to tell
        # "not set up" from "set up and broken" - and the three GitHub things
        # here are easy to confuse: signing in to the dashboard proves who you
        # are, GITHUB_ADMIN_TOKEN guards PulseGraph's own admin routes, and
        # neither grants access to a single repository. Only the App does.
        missing = [
            name
            for name, value in (
                ("GITHUB_APP_ID or GITHUB_APP_CLIENT_ID", settings.github_app_issuer),
                ("GITHUB_APP_PRIVATE_KEY", settings.GITHUB_APP_PRIVATE_KEY),
                ("GITHUB_APP_SLUG", settings.GITHUB_APP_SLUG),
                ("GITHUB_WEBHOOK_SECRET", settings.GITHUB_WEBHOOK_SECRET),
            )
            if not value
        ]
        logger.warning(
            "github_app_not_configured",
            missing=missing,
            detail=(
                "no read-only GitHub App credentials, so Code Investigation "
                "cannot read any repository. Dashboard GitHub sign-in and "
                "GITHUB_ADMIN_TOKEN are separate and do not grant repository "
                "access. See docs/github-phase1-setup.md"
            ),
        )

    # The local model is optional too. A disabled or malformed Ollama setting
    # leaves the GitHub diagnosis endpoint available for its safe fallback,
    # while alert ingestion keeps its original startup path.
    ollama_provider: OllamaLocalProvider | None = None
    hosted_provider: AnthropicDiagnosisProvider | None = None
    app.state.ollama_provider = None
    app.state.hosted_provider = None
    app.state.diagnosis_service = DiagnosisService()

    # Local first when both are configured: it is the option that keeps source
    # inside the deployment, so it should win by default rather than by luck.
    if settings.ANTHROPIC_DIAGNOSIS_ENABLED and not settings.OLLAMA_ENABLED:
        try:
            hosted_provider = AnthropicDiagnosisProvider(
                settings.ANTHROPIC_API_KEY,
                model=settings.ANTHROPIC_MODEL,
                max_tokens=settings.ANTHROPIC_MAX_OUTPUT_TOKENS,
                timeout=settings.ANTHROPIC_TIMEOUT_SECONDS,
            )
            app.state.hosted_provider = hosted_provider
            app.state.diagnosis_service = DiagnosisService(hosted_provider)
            logger.info("hosted_diagnosis_enabled", model=settings.ANTHROPIC_MODEL)
        except (AnthropicConfigurationError, ValueError) as exc:
            logger.warning("hosted_diagnosis_disabled", reason=str(exc))

    if settings.OLLAMA_ENABLED:
        try:
            ollama_provider = OllamaLocalProvider(
                settings.OLLAMA_MODEL,
                base_url=settings.OLLAMA_BASE_URL,
                timeout=settings.OLLAMA_TIMEOUT_SECONDS,
                limits=OllamaLocalLimits(
                    max_output_tokens=settings.OLLAMA_MAX_OUTPUT_TOKENS,
                    max_patch_source_files=settings.GITHUB_PATCH_MAX_FILES,
                    max_patch_source_file_bytes=settings.GITHUB_PATCH_MAX_FILE_BYTES,
                    max_patch_source_bytes=settings.GITHUB_PATCH_MAX_TOTAL_BYTES,
                    max_patch_changes=settings.GITHUB_PATCH_MAX_CHANGES,
                ),
            )
            app.state.ollama_provider = ollama_provider
            app.state.diagnosis_service = DiagnosisService(ollama_provider)
        except (OllamaLocalConfigurationError, ValueError) as exc:
            logger.warning("ollama_diagnosis_disabled", reason=str(exc))

    timer_wheel = TimerWheel()
    writer = DbWriter(timer_wheel=timer_wheel)
    timer_worker = TimerWorker(db, timer_wheel)
    recovered_deadline_count = await timer_worker.recover_persisted_deadlines()
    timer_worker.start()
    worker = OutboxWorker(
        db,
        BreakerConfig(
            failure_threshold=settings.OUTBOX_BREAKER_FAILURE_THRESHOLD,
            probe_base_seconds=settings.OUTBOX_PROBE_BASE_SECONDS,
            probe_max_seconds=settings.OUTBOX_PROBE_MAX_SECONDS,
            half_open_allowance=settings.OUTBOX_HALF_OPEN_ALLOWANCE,
        ),
    )
    worker.start()
    # Closes incidents whose alerts simply stopped, which is how most fixes
    # actually reach us. Off by config, not by omission.
    silence_sweeper = SilenceSweeper(db)
    silence_sweeper.start()
    # Root cause is an enrichment, so it is ranked off the write path and
    # debounced per scope rather than once per alert.
    root_cause_worker = RootCauseWorker(db)
    root_cause_worker.start()
    # The dead man's switch. Started last so it observes a fully-built app, and
    # deliberately given app.state so it can see whether the other workers are
    # alive - a process answering HTTP while its drain loop is dead is exactly
    # the failure it exists to catch.
    heartbeat = HeartbeatEmitter(db, app.state)
    heartbeat.start()

    app.state.ingest_credentials = parse_tokens(settings.INGEST_TOKENS)
    if settings.INGEST_AUTH_ENABLED and not app.state.ingest_credentials:
        # Loud, because the failure is silent otherwise: ingest returns 503 and
        # a misconfigured deployment looks like a broken one.
        logger.error(
            "ingest_auth_unconfigured",
            detail="INGEST_AUTH_ENABLED is true but INGEST_TOKENS is empty; "
            "ingest will refuse every request until a token is configured",
        )

    app.state.silence_sweeper = silence_sweeper
    app.state.root_cause_worker = root_cause_worker
    app.state.heartbeat = heartbeat
    app.state.db = db
    app.state.writer = writer
    app.state.worker = worker
    app.state.timer_worker = timer_worker

    logger.info(
        "app_started",
        database_path=settings.DATABASE_PATH,
        recovered_timer_deadlines=recovered_deadline_count,
    )

    try:
        yield
    finally:
        await timer_worker.stop()
        await silence_sweeper.stop()
        await root_cause_worker.stop()
        await heartbeat.stop()
        await worker.stop()
        if github_client is not None:
            await github_client.aclose()
        if ollama_provider is not None:
            await ollama_provider.aclose()
        if hosted_provider is not None:
            await hosted_provider.aclose()
        await db.close()
        logger.info("app_stopped")


app = FastAPI(title="Alert Fatigue Buster — Ingest Spine", lifespan=lifespan)
app.include_router(create_sse_router(settings.DATABASE_PATH))
app.include_router(create_github_router())
app.include_router(create_inbound_router())


def _authenticate_ingest(
    request: Request, authorization: str | None, header_token: str | None
):
    """Identify the caller, or fail the request. Returns None when auth is off."""

    if not settings.INGEST_AUTH_ENABLED:
        return None
    try:
        return authenticate(
            request.app.state.ingest_credentials, authorization, header_token
        )
    except IngestNotConfigured as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except IngestAuthError as exc:
        # Deliberately uninformative to the caller; the detail is in our logs.
        logger.warning("ingest_auth_rejected", reason=str(exc))
        raise HTTPException(status_code=401, detail="invalid ingest credential") from exc


def _authorize_event(credential, event: NormalizedEvent) -> None:
    """Confirm the source may write the scope this event lands in."""

    if credential is None:
        return
    try:
        authorize_scope(credential, scope_key_for(event))
    except IngestScopeError as exc:
        logger.warning(
            "ingest_scope_rejected",
            source=credential.name,
            service=event.service,
            reason=str(exc),
        )
        raise HTTPException(status_code=403, detail="scope not permitted") from exc


@app.post("/v1/ingest/prometheus")
async def ingest_prometheus(
    request: Request,
    authorization: str | None = Header(default=None),
    x_pulsegraph_token: str | None = Header(default=None),
):
    credential = _authenticate_ingest(request, authorization, x_pulsegraph_token)
    body = await request.json()

    try:
        events: list[NormalizedEvent] = normalize_prometheus(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid prometheus payload: {exc}")

    db: Database = request.app.state.db
    writer: DbWriter = request.app.state.writer

    # Authorise every event before writing any of them, so a batch containing
    # one out-of-scope alert cannot half-apply.
    for event in events:
        _authorize_event(credential, event)

    results = []
    for event in events:
        async with db.write_lock:
            result = await writer.process_event(db.writer_conn, event)
        results.append(result)

    return {"status": "ok", "ingested": len(results), "results": results}


async def _ingest_normalized_events(
    request: Request,
    normalizer,
    source: str,
    authorization: str | None = None,
    header_token: str | None = None,
):
    """Parse one provider's payload and send its normalized events to the writer."""

    credential = _authenticate_ingest(request, authorization, header_token)

    try:
        body = await request.json()
        events: list[NormalizedEvent] = normalizer(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid {source} payload: {exc}")

    db: Database = request.app.state.db
    writer: DbWriter = request.app.state.writer

    for event in events:
        _authorize_event(credential, event)

    results = []
    for event in events:
        async with db.write_lock:
            result = await writer.process_event(db.writer_conn, event)
        results.append(result)

    return {"status": "ok", "ingested": len(results), "results": results}


@app.post("/v1/ingest/datadog")
async def ingest_datadog(
    request: Request,
    authorization: str | None = Header(default=None),
    x_pulsegraph_token: str | None = Header(default=None),
):
    return await _ingest_normalized_events(
        request, normalize_datadog, "datadog", authorization, x_pulsegraph_token
    )


@app.post("/v1/ingest/grafana")
async def ingest_grafana(
    request: Request,
    authorization: str | None = Header(default=None),
    x_pulsegraph_token: str | None = Header(default=None),
):
    return await _ingest_normalized_events(
        request, normalize_grafana, "grafana", authorization, x_pulsegraph_token
    )


@app.get("/v1/incidents/recent")
async def incidents_recent(since: str | None = None):
    if since is None:
        since_dt = datetime.fromtimestamp(0, tz=timezone.utc)
    else:
        try:
            since_dt = datetime.fromisoformat(since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(status_code=400, detail="invalid 'since' — expected ISO8601")

    since_str = since_dt.strftime("%Y-%m-%dT%H:%M:%S.") + f"{since_dt.microsecond // 1000:03d}Z"

    async with get_reader_connection(settings.DATABASE_PATH) as conn:
        async with conn.execute(
            """
            SELECT incident_id, title, summary, severity, status, alert_count,
                   first_alert_at, last_alert_at, ewma_rate, route_decision,
                   root_cause_hint, created_at, updated_at
            FROM incidents
            WHERE updated_at > ?
            ORDER BY updated_at DESC
            """,
            (since_str,),
        ) as cursor:
            rows = await cursor.fetchall()

    return {"incidents": [dict(row) for row in rows]}


@app.get("/v1/edges/recent")
async def edges_recent(limit: int = 500):
    """Correlation edges, for the dashboard's Correlations view.

    Edges were only ever published on the SSE stream, so the panel had nothing
    to render until a live event arrived - a first page load, or a reconnect
    after sign-in, showed an empty graph even though the correlations were
    sitting in the database. The dashboard already proxied this path and
    swallowed the 404 to keep failing quiet; this makes the call mean something.

    Bounded and ordered by recency for the same reason the correlation
    neighbourhood is: the edge table grows with the square of the active
    incident set, and an unbounded read here would be the same mistake at the
    read end.
    """

    if limit < 1 or limit > 5_000:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 5000")

    async with get_reader_connection(settings.DATABASE_PATH) as conn:
        async with conn.execute(
            """
            SELECT src_incident_id, dst_incident_id, weight, last_seen_at
            FROM edges
            ORDER BY last_seen_at DESC
            LIMIT ?
            """,
            (limit,),
        ) as cursor:
            rows = await cursor.fetchall()

    return {"edges": [dict(row) for row in rows]}


@app.get("/v1/health")
async def health(request: Request):
    """Liveness only. See /v1/health/self for whether alerts are getting out."""

    db: Database = request.app.state.db
    try:
        async with db.write_lock:
            await db.writer_conn.execute("SELECT 1")
        return {"status": "healthy"}
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)}


@app.get("/v1/health/self")
async def health_self(request: Request):
    """What the alerting system knows about itself.

    Every signal here was already being recorded and none of it was reachable,
    so an operator's only view of PulseGraph was whether its HTTP port
    answered - which stays true while the outbox stalls and nothing is
    delivered. This reports on *delivery*, which is the job.
    """

    db: Database = request.app.state.db
    signals = await gather(db, request.app.state)
    report = evaluate(
        signals,
        SelfCheckThresholds(
            stuck_outbox_seconds=settings.SELFCHECK_STUCK_OUTBOX_SECONDS,
            dead_letter_limit=settings.SELFCHECK_DEAD_LETTER_LIMIT,
            quiet_ingest_seconds=settings.SELFCHECK_QUIET_INGEST_SECONDS,
            clock_skew_ms=settings.CLOCK_SKEW_WARN_MS,
        ),
    )

    emitter: HeartbeatEmitter | None = getattr(request.app.state, "heartbeat", None)
    return {
        "verdict": report.verdict.value,
        "reasons": list(report.reasons),
        "observations": list(report.observations),
        "signals": {
            "database_reachable": signals.database_reachable,
            "workers": signals.workers,
            "outbox_pending": signals.outbox_pending,
            "outbox_dead": signals.outbox_dead,
            "oldest_pending_age_seconds": signals.oldest_pending_age_seconds,
            "open_channels": list(signals.open_channels),
            "ongoing_outages": signals.ongoing_outages,
            "seconds_since_last_ingest": signals.seconds_since_last_ingest,
            "worst_clock_skew_ms": signals.worst_clock_skew_ms,
            "unranked_scopes": signals.unranked_scopes,
        },
        # Whether anything outside would actually notice this process dying.
        "dead_mans_switch": {
            "configured": bool(emitter and emitter.enabled),
            "running": bool(emitter and emitter.running),
            "last_verdict": (
                emitter.last_verdict.value
                if emitter and emitter.last_verdict
                else None
            ),
            "last_send_ok": emitter.last_sent_ok if emitter else None,
            "suppressed_beats": emitter.suppressed_count if emitter else 0,
        },
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
