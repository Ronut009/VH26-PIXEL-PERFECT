"""Pure, scope-aware exact-deduplication helpers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import hmac
import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from src.contracts import NormalizedEvent


_VOLATILE_FIELD_NAMES = frozenset(
    {
        "timestamp",
        "fired_at",
        "starts_at",
        "ends_at",
        "event_id",
        "source_event_id",
        "pod",
        "pod_name",
        "pod_uid",
        "uid",
        "instance",
        "container_id",
        "containerid",
    }
)


def _normalise_key(key: object) -> str:
    return str(key).strip().lower().replace("-", "_")


def _is_volatile_key(key: object) -> bool:
    normalized = _normalise_key(key)
    return (
        normalized in _VOLATILE_FIELD_NAMES
        or normalized.endswith("_timestamp")
        or normalized.endswith("_pod_uid")
    )


def _as_dict(event: NormalizedEvent | Mapping[str, Any]) -> dict[str, Any]:
    model_dump = getattr(event, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json")
    if isinstance(event, Mapping):
        return dict(event)
    raise TypeError("event must be a NormalizedEvent or mapping")


def _stable_labels(labels: object) -> dict[str, str]:
    if not isinstance(labels, Mapping):
        return {}
    return {
        str(key): str(value)
        for key, value in labels.items()
        if not _is_volatile_key(key)
    }


def _fingerprint_material(event: NormalizedEvent | Mapping[str, Any]) -> dict[str, Any]:
    payload = _as_dict(event)
    return {
        "service": str(payload.get("service", "")),
        "alertname": str(payload.get("alertname", "")),
        "severity": str(payload.get("severity", payload.get("severity_raw", ""))),
        "status": str(payload.get("status", "")),
        "labels": _stable_labels(payload.get("labels", {})),
    }


def generate_fingerprint(event: NormalizedEvent | Mapping[str, Any]) -> str:
    """Return a deterministic hash of stable alert identity fields only."""

    canonical = json.dumps(
        _fingerprint_material(event),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _event_scope(event: Mapping[str, Any], fallback_scope: str) -> str:
    value = event.get("scope_key", fallback_scope)
    return str(value)


def is_exact_duplicate(
    event_a: dict[str, Any], event_b: dict[str, Any], scope_key: str
) -> bool:
    """Return whether two alerts have the same stable identity in one scope.

    Each event may optionally carry ``scope_key``. When it does, it must match
    the caller-provided scope; this prevents an identical alert in staging from
    deduplicating a production incident.
    """

    if not scope_key:
        return False
    if _event_scope(event_a, scope_key) != scope_key:
        return False
    if _event_scope(event_b, scope_key) != scope_key:
        return False

    return hmac.compare_digest(
        generate_fingerprint(event_a), generate_fingerprint(event_b)
    )
