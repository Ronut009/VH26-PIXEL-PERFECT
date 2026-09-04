"""HTTP endpoints for provider callbacks - the way state flows back in.

Both routes follow the same shape, which is deliberate:

    verify signature -> parse into an ExternalAction -> apply in one
    transaction -> answer the provider fast

They answer 200 for anything they successfully verified, including callbacks
they chose to ignore. Providers retry on non-2xx and eventually disable an
endpoint that keeps failing, so "I understood you and decided this changes
nothing" must not look like an error.
"""

from __future__ import annotations

import json
from urllib.parse import parse_qs

from fastapi import APIRouter, HTTPException, Request

from src.config import settings
from src.inbound import reconcile
from src.inbound.reconcile import ACKNOWLEDGE, RESOLVE, ExternalAction
from src.inbound.signatures import (
    SignatureError,
    payload_digest,
    verify_pagerduty,
    verify_slack,
)
from src.utils.logging import get_logger

logger = get_logger(__name__)

# Callbacks are small. A body larger than this is not a real Slack click or
# PagerDuty webhook, and reading it would just be free memory pressure.
MAX_CALLBACK_BYTES = 256 * 1024

# PagerDuty v3 event types we act on. Everything else is acknowledged and
# dropped - we do not want to invent behaviour for events we did not design for.
_PAGERDUTY_KINDS = {
    "incident.acknowledged": ACKNOWLEDGE,
    "incident.resolved": RESOLVE,
}

_SLACK_ACTION_KINDS = {
    "acknowledge_incident": ACKNOWLEDGE,
    "resolve_incident": RESOLVE,
}


async def _read_body(request: Request) -> bytes:
    body = await request.body()
    if len(body) > MAX_CALLBACK_BYTES:
        raise HTTPException(status_code=413, detail="callback too large")
    return body


async def _apply(request: Request, action: ExternalAction) -> dict:
    """Run one reconciliation inside its own write transaction."""

    db = request.app.state.db
    async with db.write_lock:
        conn = db.writer_conn
        if conn is None:
            raise HTTPException(status_code=503, detail="database unavailable")
        await conn.execute("BEGIN IMMEDIATE")
        try:
            result = await reconcile.apply_external_action(conn, action)
            await conn.commit()
        except Exception:
            await conn.rollback()
            raise

    logger.info(
        "inbound_action",
        provider=action.provider,
        kind=action.kind,
        incident_id=action.incident_id,
        status=result.status,
        from_state=result.from_state,
        to_state=result.to_state,
    )
    return {"status": result.status, "incident_id": result.incident_id}


def create_inbound_router() -> APIRouter:
    router = APIRouter(tags=["inbound"])

    @router.post("/v1/slack/interactions")
    async def slack_interactions(request: Request):
        body = await _read_body(request)
        try:
            verify_slack(
                settings.SLACK_SIGNING_SECRET,
                request.headers.get("X-Slack-Request-Timestamp", ""),
                request.headers.get("X-Slack-Signature", ""),
                body,
            )
        except SignatureError as exc:
            logger.warning("slack_signature_rejected", reason=str(exc))
            raise HTTPException(status_code=401, detail="invalid signature") from exc

        # Slack posts interactions form-encoded, with the interesting part in
        # a JSON string under `payload`.
        form = parse_qs(body.decode("utf-8", errors="replace"))
        raw_payload = (form.get("payload") or [""])[0]
        try:
            payload = json.loads(raw_payload)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="malformed payload") from exc

        actions = payload.get("actions") or []
        if not actions:
            return {"status": "ignored", "detail": "no actions"}

        first = actions[0]
        kind = _SLACK_ACTION_KINDS.get(first.get("action_id", ""))
        incident_id = first.get("value") or ""
        if kind is None or not incident_id:
            return {"status": "ignored", "detail": "unhandled action"}

        # trigger_id is unique per interaction, so a double-click is caught by
        # the inbound_events primary key rather than transitioning twice.
        inbound_id = payload.get("trigger_id") or f"slack:{incident_id}:{kind}"

        return await _apply(
            request,
            ExternalAction(
                inbound_id=f"slack:{inbound_id}",
                provider="slack",
                kind=kind,
                incident_id=incident_id,
                actor=(payload.get("user") or {}).get("username")
                or (payload.get("user") or {}).get("id"),
                detail="slack interactive action",
                payload_sha256=payload_digest(body),
            ),
        )

    @router.post("/v1/pagerduty/webhooks")
    async def pagerduty_webhooks(request: Request):
        body = await _read_body(request)
        try:
            verify_pagerduty(
                settings.PAGERDUTY_WEBHOOK_SECRET,
                request.headers.get("X-PagerDuty-Signature", ""),
                body,
            )
        except SignatureError as exc:
            logger.warning("pagerduty_signature_rejected", reason=str(exc))
            raise HTTPException(status_code=401, detail="invalid signature") from exc

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise HTTPException(status_code=400, detail="malformed payload") from exc

        event = payload.get("event") or {}
        kind = _PAGERDUTY_KINDS.get(event.get("event_type", ""))
        if kind is None:
            return {"status": "ignored", "detail": "unhandled event type"}

        data = event.get("data") or {}
        # We set dedup_key to our own incident id when triggering, so it is
        # normally right there. When PagerDuty omits it, the outbox row that
        # created the PagerDuty incident is the identity map back.
        incident_id = data.get("dedup_key") or ""
        db = request.app.state.db
        if not incident_id and data.get("id"):
            async with db.write_lock:
                incident_id = (
                    await reconcile.resolve_incident_id_from_ref(
                        db.writer_conn, "pagerduty", str(data["id"])
                    )
                    or ""
                )

        if not incident_id:
            return {"status": "ignored", "detail": "unmapped pagerduty incident"}

        agent = event.get("agent") or {}
        return await _apply(
            request,
            ExternalAction(
                inbound_id=f"pagerduty:{event.get('id') or incident_id}",
                provider="pagerduty",
                kind=kind,
                incident_id=incident_id,
                actor=agent.get("summary") or agent.get("id"),
                detail=event.get("event_type"),
                payload_sha256=payload_digest(body),
            ),
        )

    return router


__all__ = ["MAX_CALLBACK_BYTES", "create_inbound_router"]
