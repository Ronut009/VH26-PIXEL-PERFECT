from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
import sys

from fastapi import FastAPI, HTTPException, Request


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import settings
from src.contracts import NormalizedEvent
from src.db.connection import Database, get_reader_connection
from src.db.writer import DbWriter
from src.engine.timer_wheel import TimerWheel
from src.engine.timer_worker import TimerWorker
from src.ingest.normalize_datadog import normalize_datadog
from src.ingest.normalize_grafana import normalize_grafana
from src.ingest.prometheus import normalize_prometheus
from src.outbox.worker import OutboxWorker
from src.stream.sse_broker import create_sse_router
from src.utils.logging import configure_logging, get_logger

configure_logging()
logger = get_logger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db = Database(settings.DATABASE_PATH)
    await db.connect()

    timer_wheel = TimerWheel()
    writer = DbWriter(timer_wheel=timer_wheel)
    timer_worker = TimerWorker(db, timer_wheel)
    recovered_deadline_count = await timer_worker.recover_persisted_deadlines()
    timer_worker.start()
    worker = OutboxWorker(db)
    worker.start()

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
        await worker.stop()
        await db.close()
        logger.info("app_stopped")


app = FastAPI(title="Alert Fatigue Buster — Ingest Spine", lifespan=lifespan)
app.include_router(create_sse_router(settings.DATABASE_PATH))


@app.post("/v1/ingest/prometheus")
async def ingest_prometheus(request: Request):
    body = await request.json()

    try:
        events: list[NormalizedEvent] = normalize_prometheus(body)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"invalid prometheus payload: {exc}")

    db: Database = request.app.state.db
    writer: DbWriter = request.app.state.writer

    results = []
    for event in events:
        async with db.write_lock:
            result = await writer.process_event(db.writer_conn, event)
        results.append(result)

    return {"status": "ok", "ingested": len(results), "results": results}


async def _ingest_normalized_events(request: Request, normalizer, source: str):
    """Parse one provider's payload and send its normalized events to the writer."""

    try:
        body = await request.json()
        events: list[NormalizedEvent] = normalizer(body)
    except Exception as exc:
        raise HTTPException(status_code=422, detail=f"invalid {source} payload: {exc}")

    db: Database = request.app.state.db
    writer: DbWriter = request.app.state.writer

    results = []
    for event in events:
        async with db.write_lock:
            result = await writer.process_event(db.writer_conn, event)
        results.append(result)

    return {"status": "ok", "ingested": len(results), "results": results}


@app.post("/v1/ingest/datadog")
async def ingest_datadog(request: Request):
    return await _ingest_normalized_events(request, normalize_datadog, "datadog")


@app.post("/v1/ingest/grafana")
async def ingest_grafana(request: Request):
    return await _ingest_normalized_events(request, normalize_grafana, "grafana")


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


@app.get("/v1/health")
async def health(request: Request):
    db: Database = request.app.state.db
    try:
        async with db.write_lock:
            await db.writer_conn.execute("SELECT 1")
        return {"status": "healthy"}
    except Exception as exc:
        return {"status": "unhealthy", "error": str(exc)}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="127.0.0.1", port=8000)
