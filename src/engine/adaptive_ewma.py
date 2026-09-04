"""Pure adaptive-EWMA silence-window calculation."""

import math


def _validate_gap(gap: float) -> float:
    numeric_gap = float(gap)
    if not math.isfinite(numeric_gap) or numeric_gap < 0:
        raise ValueError("gap values must be finite and non-negative")
    return numeric_gap


def calculate_quiet_deadline(
    gap_history: list[float], last_gap: float, current_time: int
) -> dict[str, float | int]:
    """Predict an adaptive quiet deadline from observed inter-arrival gaps.

    The latest gap is appended to the history and smoothed with an EWMA whose
    gain is derived from the available observation count: ``2 / (n + 1)``.
    The silence window is the predicted mean gap plus its observed uncertainty.
    No wall-clock batching interval or side effect is involved.
    """

    gaps = [_validate_gap(gap) for gap in gap_history]
    gaps.append(_validate_gap(last_gap))

    observation_count = len(gaps)
    alpha = 2.0 / (observation_count + 1)

    mean_gap = gaps[0]
    variance = 0.0
    for gap in gaps[1:]:
        previous_mean = mean_gap
        mean_gap = ((1.0 - alpha) * mean_gap) + (alpha * gap)
        variance = (1.0 - alpha) * (
            variance + (alpha * (gap - previous_mean) ** 2)
        )

    silence_window = mean_gap + math.sqrt(variance)
    quiet_at_ms = int(math.ceil(current_time + silence_window))

    return {
        "quiet_at_ms": quiet_at_ms,
        "mean_gap": mean_gap,
        "variance": variance,
    }
