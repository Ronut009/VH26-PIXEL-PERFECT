"""SMTP delivery for the third hop of the failover chain.

Email is the fallback that works when both Slack and PagerDuty are gone. It is
the slowest and least interruptive channel, which is exactly why it sits last:
nothing here should ever be the *primary* route for an urgent alert.

Two deliberate choices:

*Standard library, no new dependency.* ``smtplib`` covers Gmail, Office 365,
Amazon SES and any corporate relay through one configuration. Reaching for an
API-specific SDK would tie the last-resort channel to one vendor's uptime,
which defeats the point of having a last resort.

*Blocking I/O is pushed to a thread.* ``smtplib`` is synchronous, and an SMTP
handshake against an unreachable relay can hang for the full socket timeout.
Running it inline would block the event loop - stalling ingest, the timer
wheel, and every other channel's delivery - during precisely the outage the
fallback exists to survive. ``asyncio.to_thread`` keeps that contained.
"""

from __future__ import annotations

import asyncio
from email.message import EmailMessage
from email.utils import formataddr, make_msgid
import smtplib
import ssl

from src.config import settings
from src.outbox.failure_policy import ChannelUnavailable, MessageRejected
from src.utils.logging import get_logger

logger = get_logger(__name__)

_SEVERITY_PREFIX = {
    "critical": "[CRITICAL]",
    "high": "[HIGH]",
    "medium": "[MEDIUM]",
    "low": "[LOW]",
}


class EmailNotConfigured(Exception):
    """Raised when the channel is selected but has no usable configuration."""


def recipients() -> list[str]:
    return [
        address.strip()
        for address in settings.EMAIL_TO.split(",")
        if address.strip()
    ]


def is_configured() -> bool:
    return bool(settings.SMTP_HOST and settings.EMAIL_FROM and recipients())


def _subject(payload: dict) -> str:
    prefix = _SEVERITY_PREFIX.get(payload.get("severity", "medium"), "[ALERT]")
    if payload.get("kind") == "recovery_digest":
        return f"[RECOVERED] {payload.get('channel', 'channel')} delivery restored"
    title = payload.get("title") or "Incident"
    if payload.get("state") == "RESOLVED":
        return f"[RESOLVED] {title}"
    return f"{prefix} {title}"


def _body(payload: dict) -> str:
    """Plain text on purpose - this may be read on a phone lock screen."""

    if payload.get("kind") == "recovery_digest":
        minutes = max(1, round(payload.get("duration_seconds", 0) / 60))
        return (
            f"{payload.get('channel', 'A channel')} was unreachable for about "
            f"{minutes} minute(s).\n\n"
            f"Incidents changed during the gap: {payload.get('incidents_touched', 0)}\n"
            f"Critical among them:           {payload.get('critical_incidents', 0)}\n"
            f"Resolved on their own:         {payload.get('resolved_during_outage', 0)}\n"
            f"Redundant updates collapsed:   {payload.get('collapsed_messages', 0)}\n"
            f"Delivered via fallback:        {payload.get('delivered_via_fallback', 0)}\n"
        )

    lines = [
        payload.get("title") or "Incident",
        "",
        payload.get("summary") or "No summary available.",
        "",
        f"Severity:    {payload.get('severity', 'unknown')}",
        f"State:       {payload.get('state', 'unknown')}",
        f"Alerts:      {payload.get('alert_count', 1)}",
        f"Incident:    {payload.get('incident_id', 'unknown')}",
    ]

    if payload.get("root_cause_hint"):
        lines += ["", f"Likely root cause: {payload['root_cause_hint']}"]

    group = payload.get("group")
    if group:
        lines += [
            "",
            f"Correlated storm: {group.get('member_count', 0)} incidents, "
            f"{group.get('total_alert_count', 0)} alerts total",
        ]
        for member in group.get("members", [])[:10]:
            lines.append(
                f"  - {member['title']} ({member['alert_count']} alerts, "
                f"{member['status']})"
            )

    if payload.get("failover_from"):
        lines += [
            "",
            f"NOTE: sent by email because {payload['failover_from']} was "
            "unreachable. This is not a separate incident.",
        ]

    if payload.get("resolution_source") == "inferred_silence":
        lines += [
            "",
            "NOTE: presumed resolved because alerts stopped arriving. "
            "Not confirmed by a human.",
        ]

    return "\n".join(lines)


def _build_message(payload: dict, to: list[str]) -> EmailMessage:
    message = EmailMessage()
    message["Subject"] = _subject(payload)
    message["From"] = formataddr(("PulseGraph", settings.EMAIL_FROM))
    message["To"] = ", ".join(to)
    message["Message-ID"] = make_msgid(domain="pulsegraph.local")

    # Thread every message about one incident together, so a mail client shows
    # a conversation per incident rather than a pile of unrelated alerts.
    incident_id = payload.get("incident_id")
    if incident_id:
        thread_ref = f"<incident-{incident_id}@pulsegraph.local>"
        message["References"] = thread_ref
        message["In-Reply-To"] = thread_ref

    message.set_content(_body(payload))
    return message


def _send_blocking(message: EmailMessage, to: list[str]) -> None:
    """Runs on a worker thread. Raises the typed failures the outbox expects."""

    context = ssl.create_default_context()
    timeout = settings.SMTP_TIMEOUT_SECONDS

    try:
        if settings.SMTP_USE_SSL:
            server = smtplib.SMTP_SSL(
                settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout, context=context
            )
        else:
            server = smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout)
    except (OSError, smtplib.SMTPException) as exc:
        # Could not even reach the relay: a channel problem, not this message's.
        raise ChannelUnavailable(f"smtp_connect:{type(exc).__name__}") from exc

    try:
        if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
            server.starttls(context=context)
        if settings.SMTP_USERNAME:
            server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
        server.send_message(message, to_addrs=to)
    except smtplib.SMTPAuthenticationError as exc:
        # Bad credentials will fail identically for every queued message, so
        # this is the channel being unusable rather than one bad payload.
        raise ChannelUnavailable("smtp_auth") from exc
    except smtplib.SMTPRecipientsRefused as exc:
        raise MessageRejected("smtp_recipients_refused") from exc
    except smtplib.SMTPSenderRefused as exc:
        raise MessageRejected("smtp_sender_refused") from exc
    except (OSError, smtplib.SMTPException) as exc:
        raise ChannelUnavailable(f"smtp_send:{type(exc).__name__}") from exc
    finally:
        try:
            server.quit()
        except Exception:
            # A relay that drops the connection after accepting the message has
            # still delivered it; failing here would re-send a mail that landed.
            pass


async def probe() -> None:
    """Reachability check that sends no mail.

    ``NOOP`` is the SMTP equivalent of Slack's ``auth.test``: it proves the
    relay is answering and the credentials work without putting anything in
    anyone's inbox, so the breaker can poll a dead relay cheaply.
    """

    if not is_configured():
        raise ChannelUnavailable("smtp_not_configured")

    def _noop() -> None:
        context = ssl.create_default_context()
        timeout = settings.SMTP_TIMEOUT_SECONDS
        try:
            if settings.SMTP_USE_SSL:
                server = smtplib.SMTP_SSL(
                    settings.SMTP_HOST,
                    settings.SMTP_PORT,
                    timeout=timeout,
                    context=context,
                )
            else:
                server = smtplib.SMTP(
                    settings.SMTP_HOST, settings.SMTP_PORT, timeout=timeout
                )
            try:
                if settings.SMTP_USE_TLS and not settings.SMTP_USE_SSL:
                    server.starttls(context=context)
                if settings.SMTP_USERNAME:
                    server.login(settings.SMTP_USERNAME, settings.SMTP_PASSWORD)
                server.noop()
            finally:
                try:
                    server.quit()
                except Exception:
                    pass
        except (OSError, smtplib.SMTPException) as exc:
            raise ChannelUnavailable(f"smtp_probe:{type(exc).__name__}") from exc

    await asyncio.to_thread(_noop)


async def send(action: str, payload: dict, external_ref: str | None) -> str:
    """Deliver one incident notification by email.

    Email cannot edit a message that was already sent, so ``update`` and
    ``resolve`` are new mails rather than edits. The ``References`` header
    threads them onto the original, which is the closest equivalent a mail
    client offers.
    """

    if not is_configured():
        # Not a transient failure: no amount of retrying will configure SMTP.
        raise ChannelUnavailable("smtp_not_configured")

    to = recipients()
    message = _build_message(payload, to)
    await asyncio.to_thread(_send_blocking, message, to)

    logger.info(
        "email_delivered",
        action=action,
        incident_id=payload.get("incident_id"),
        recipients=len(to),
    )
    # The Message-ID is this channel's external_ref, so a later mail about the
    # same incident can thread onto it.
    return message["Message-ID"]


__all__ = ["EmailNotConfigured", "is_configured", "probe", "recipients", "send"]
