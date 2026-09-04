"""Pure adaptive-EWMA silence-window contract."""


def calculate_quiet_deadline(
    gap_history: list[float], last_gap: float
) -> dict[str, float]:
    """Calculate data-derived cadence statistics and a quiet deadline.

    Inputs and outputs are numeric only so Phase 3 can be tested without a
    clock, database connection, or notification channel.
    """

    raise NotImplementedError("Phase 3: implement adaptive EWMA")
