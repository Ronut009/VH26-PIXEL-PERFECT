"""Pure adaptive-EWMA silence-window calculation."""

import math


DEFAULT_INITIAL_WINDOW_MS = 5_000
# A single predicted silence window is never allowed past this. Without a
# ceiling, one pathologically slow-flapping alert can predict a window of
# hours and hold its incident undelivered for that long.
DEFAULT_MAX_WINDOW_MS = 300_000
# How many inter-arrival gaps one incident keeps. This is not a storage
# nicety: the EWMA gain below is 2/(n+1), so an unbounded history drives the
# gain toward zero and the window stops adapting -- after a few hundred alerts
# a genuine change in cadence barely moves the mean, which is the opposite of
# what an adaptive window is for. Bounding n turns it into a true sliding
# EWMA with a fixed floor on its gain (2/21 at 20 gaps), and incidentally
# bounds what one incident costs to rewrite on every alert.
DEFAULT_GAP_HISTORY_MAX = 20


def retain_recent_gaps(
    gap_history: list[float], limit: int | None = None
) -> list[float]:
    """Keep only the most recent gaps, preserving order.

    Applied where the history grows, so a row written by an older version
    shrinks to the window on its next alert rather than needing a migration.
    """

    window = DEFAULT_GAP_HISTORY_MAX if limit is None else int(limit)
    if window <= 0:
        raise ValueError("gap history limit must be greater than zero")
    return list(gap_history[-window:])


def _validate_gap(gap: float) -> float:
    numeric_gap = float(gap)
    if not math.isfinite(numeric_gap) or numeric_gap < 0:
        raise ValueError("gap values must be finite and non-negative")
    return numeric_gap


def calculate_quiet_deadline(
    gap_history: list[float],
    last_gap: float,
    current_time: int,
    max_window_ms: float | None = None,
) -> dict[str, float | int]:
    """Predict an adaptive quiet deadline from observed inter-arrival gaps.

    The latest gap is appended to the history and smoothed with an EWMA whose
    gain is derived from the available observation count: ``2 / (n + 1)``.
    The silence window is the predicted mean gap plus its observed uncertainty.
    No wall-clock batching interval or side effect is involved.

    ``max_window_ms`` bounds one prediction so a noisy signal cannot defer its
    own delivery indefinitely. It caps the window, not the incident: the caller
    is responsible for the absolute ceiling measured from first alert.
    """

    gaps = [_validate_gap(gap) for gap in gap_history]
    validated_last_gap = _validate_gap(last_gap)

    ceiling = DEFAULT_MAX_WINDOW_MS if max_window_ms is None else float(max_window_ms)
    if ceiling <= 0:
        raise ValueError("max_window_ms must be greater than zero")

    if not gaps:
        return {
            "quiet_at_ms": int(current_time + min(DEFAULT_INITIAL_WINDOW_MS, ceiling)),
            "mean_gap": validated_last_gap,
            "variance": 0.0,
        }

    gaps.append(validated_last_gap)

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

    silence_window = min(mean_gap + math.sqrt(variance), ceiling)
    quiet_at_ms = int(current_time + max(1, math.ceil(silence_window)))

    return {
        "quiet_at_ms": quiet_at_ms,
        "mean_gap": mean_gap,
        "variance": variance,
    }
