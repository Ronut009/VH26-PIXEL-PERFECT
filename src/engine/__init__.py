"""Pure incident-engine building blocks for PulseGraph.

The package intentionally has no database, HTTP, or queue dependencies.
DbWriter will inject a transaction into the Phase 4 integration wrapper.
"""

from .incident_machine import IncidentState

__all__ = ["IncidentState"]
