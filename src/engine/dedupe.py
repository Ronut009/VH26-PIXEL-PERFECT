"""Pure fingerprint helpers.

Phase 3 will align generated fingerprints with src.utils.fingerprint without
opening a database connection or mutating an event.
"""

from src.contracts import NormalizedEvent


def generate_fingerprint(event: NormalizedEvent) -> str:
    """Return the stable deduplication key for a normalized event.

    This Phase 1 stub intentionally performs no computation. The eventual
    implementation must remain deterministic and side-effect free.
    """

    raise NotImplementedError("Phase 3: implement fingerprint generation")
