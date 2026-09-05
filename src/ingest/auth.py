"""Authentication and scope authorisation for alert ingestion.

Ingest was the one endpoint that trusted anyone. GitHub webhooks verify an
HMAC, and the Slack and PagerDuty callbacks fail closed without a signing
secret - but the endpoint that *creates incidents* accepted anything from
anywhere.

The interesting attack is not a flood, it is a forgery. Dedupe keys on
``service | alertname | severity | stable labels``, so anyone who could reach
the URL could post a ``resolved`` alert whose labels match a real, firing
incident, and that forged resolve would land on the real incident and close
it. One unauthenticated request silences a production emergency - the exact
inverse of what an alerting system is for.

Two checks, because authentication alone is not enough:

*Who are you.* A bearer token, compared in constant time. Bearer rather than
body HMAC because Alertmanager can send an ``Authorization`` header natively
but cannot sign a request body - a scheme the sender cannot implement is a
scheme that ends up disabled.

*What are you allowed to say.* Every credential is bound to a scope prefix. A
token issued for staging cannot write - or resolve - an incident in
``production/eu-west``, so a leaked staging token cannot silence production.
Scope is already the identity boundary the engine dedupes within, so this is
an authorisation check rather than new modelling.
"""

from __future__ import annotations

from dataclasses import dataclass
import hmac

from src.utils.logging import get_logger

logger = get_logger(__name__)


class IngestAuthError(Exception):
    """Raised when a caller cannot be identified. Maps to 401."""


class IngestScopeError(Exception):
    """Raised when a known caller writes outside its scope. Maps to 403."""


class IngestNotConfigured(Exception):
    """Raised when auth is required but no credentials exist. Maps to 503."""


@dataclass(frozen=True)
class IngestCredential:
    """One configured alert source."""

    name: str
    token: str
    # Scope prefix this source may write. "*" means any scope, which is the
    # right setting for a single-environment deployment and the wrong one for
    # anything with a staging environment pointed at the same backend.
    scope: str = "*"

    def may_write(self, scope_key: str) -> bool:
        if self.scope == "*":
            return True
        prefix = self.scope[:-1] if self.scope.endswith("*") else self.scope
        return scope_key.startswith(prefix)


def parse_tokens(raw: str) -> tuple[IngestCredential, ...]:
    """Parse ``name:token[:scope]`` entries, comma-separated.

    A malformed entry is dropped with a warning rather than failing startup:
    one typo in a multi-source list should not take alert ingestion offline
    for every other source.
    """

    credentials: list[IngestCredential] = []
    for entry in raw.split(","):
        entry = entry.strip()
        if not entry:
            continue
        parts = entry.split(":")
        if len(parts) < 2 or not parts[0].strip() or not parts[1].strip():
            logger.warning("ingest_token_malformed", entry_prefix=entry[:16])
            continue
        name, token = parts[0].strip(), parts[1].strip()
        scope = parts[2].strip() if len(parts) > 2 and parts[2].strip() else "*"
        credentials.append(IngestCredential(name=name, token=token, scope=scope))
    return tuple(credentials)


def _presented_token(authorization: str | None, header_token: str | None) -> str:
    """Read the token from either header Alertmanager-style senders can set."""

    if authorization:
        scheme, _, value = authorization.partition(" ")
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()
    return (header_token or "").strip()


def authenticate(
    credentials: tuple[IngestCredential, ...],
    authorization: str | None,
    header_token: str | None,
) -> IngestCredential:
    """Identify the caller, or raise.

    Compares against every configured credential rather than short-circuiting
    on the first mismatch, so response time does not leak which token prefix
    was close.
    """

    if not credentials:
        raise IngestNotConfigured(
            "ingest authentication is enabled but INGEST_TOKENS is empty"
        )

    presented = _presented_token(authorization, header_token)
    if not presented:
        raise IngestAuthError("missing ingest credential")

    matched: IngestCredential | None = None
    for credential in credentials:
        if hmac.compare_digest(credential.token, presented):
            matched = credential

    if matched is None:
        raise IngestAuthError("unrecognised ingest credential")
    return matched


def authorize_scope(credential: IngestCredential, scope_key: str) -> None:
    """Confirm this source may write this scope, or raise."""

    if not credential.may_write(scope_key):
        raise IngestScopeError(
            f"source '{credential.name}' may not write scope '{scope_key}'"
        )


__all__ = [
    "IngestAuthError",
    "IngestCredential",
    "IngestNotConfigured",
    "IngestScopeError",
    "authenticate",
    "authorize_scope",
    "parse_tokens",
]
