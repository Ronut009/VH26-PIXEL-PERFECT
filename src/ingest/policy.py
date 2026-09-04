from src.contracts import NormalizedEvent


def critical_bypass(event: NormalizedEvent) -> bool:
    return event.labels.get("severity") == "critical" or event.labels.get("priority") == "P0"
