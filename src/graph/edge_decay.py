"""Pure exponential-decay math for directed co-occurrence evidence."""

from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class DecayedWeights:
    joint: float
    source: float
    target: float


def _validate_non_negative(value: float, field_name: str) -> float:
    numeric = float(value)
    if not math.isfinite(numeric) or numeric < 0:
        raise ValueError(f"{field_name} must be finite and non-negative")
    return numeric


def _decay(value: float, elapsed_ms: float, half_life_ms: float) -> float:
    return value * math.exp(-math.log(2.0) * elapsed_ms / half_life_ms)


def decay_weights(
    joint_weight: float,
    source_weight: float,
    target_weight: float,
    elapsed_ms: float,
    half_life_ms: float,
) -> DecayedWeights:
    """Decay every evidence counter by the same elapsed-time factor."""

    joint = _validate_non_negative(joint_weight, "joint_weight")
    source = _validate_non_negative(source_weight, "source_weight")
    target = _validate_non_negative(target_weight, "target_weight")
    elapsed = _validate_non_negative(elapsed_ms, "elapsed_ms")
    half_life = _validate_non_negative(half_life_ms, "half_life_ms")
    if half_life == 0:
        raise ValueError("half_life_ms must be greater than zero")

    return DecayedWeights(
        joint=_decay(joint, elapsed, half_life),
        source=_decay(source, elapsed, half_life),
        target=_decay(target, elapsed, half_life),
    )


def increment_weights(
    previous: DecayedWeights,
    *,
    elapsed_ms: float,
    half_life_ms: float,
    joint_increment: float = 1.0,
    source_increment: float = 1.0,
    target_increment: float = 1.0,
) -> DecayedWeights:
    """Decay prior evidence, then add one new directed observation."""

    decayed = decay_weights(
        previous.joint,
        previous.source,
        previous.target,
        elapsed_ms,
        half_life_ms,
    )
    return DecayedWeights(
        joint=decayed.joint + _validate_non_negative(joint_increment, "joint_increment"),
        source=decayed.source + _validate_non_negative(source_increment, "source_increment"),
        target=decayed.target + _validate_non_negative(target_increment, "target_increment"),
    )
