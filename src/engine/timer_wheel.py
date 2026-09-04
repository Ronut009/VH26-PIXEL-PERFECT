"""Thread-safe, priority-queue scheduling for adaptive quiet deadlines."""

from __future__ import annotations

from dataclasses import dataclass
import heapq
from itertools import count
from threading import RLock
from uuid import UUID


@dataclass(frozen=True)
class TimerTrigger:
    """A due lifecycle command for the transaction owner to apply."""

    incident_id: UUID
    quiet_at_ms: int
    trigger: str = "QUIET_DEADLINE"


class TimerWheel:
    """Schedule adaptive deadlines with O(log n) insert and removal work.

    Re-scheduling an incident leaves its obsolete heap entry in place and
    invalidates it using a generation number. This avoids a linear heap scan;
    stale entries are discarded lazily when they become due.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._heap: list[tuple[int, int, UUID, int]] = []
        self._active: dict[UUID, tuple[int, int]] = {}
        self._sequence = count()

    def schedule(self, incident_id: UUID, quiet_at_ms: int) -> None:
        """Insert or replace one incident's dynamic quiet deadline."""

        if quiet_at_ms < 0:
            raise ValueError("quiet_at_ms must be non-negative")

        with self._lock:
            previous = self._active.get(incident_id)
            generation = 1 if previous is None else previous[1] + 1
            self._active[incident_id] = (quiet_at_ms, generation)
            heapq.heappush(
                self._heap,
                (quiet_at_ms, next(self._sequence), incident_id, generation),
            )

    def cancel(self, incident_id: UUID) -> bool:
        """Cancel an active deadline without scanning the heap."""

        with self._lock:
            return self._active.pop(incident_id, None) is not None

    def pop_due(self, now_ms: int) -> tuple[TimerTrigger, ...]:
        """Return due, non-stale QUIET_DEADLINE commands in deadline order."""

        due: list[TimerTrigger] = []
        with self._lock:
            while self._heap and self._heap[0][0] <= now_ms:
                quiet_at_ms, _sequence, incident_id, generation = heapq.heappop(self._heap)
                if self._active.get(incident_id) != (quiet_at_ms, generation):
                    continue
                del self._active[incident_id]
                due.append(TimerTrigger(incident_id=incident_id, quiet_at_ms=quiet_at_ms))
        return tuple(due)

    def next_deadline_ms(self) -> int | None:
        """Return the next valid deadline without blocking a worker thread."""

        with self._lock:
            while self._heap:
                quiet_at_ms, _sequence, incident_id, generation = self._heap[0]
                if self._active.get(incident_id) == (quiet_at_ms, generation):
                    return quiet_at_ms
                heapq.heappop(self._heap)
            return None

    def __len__(self) -> int:
        with self._lock:
            return len(self._active)


__all__ = ["TimerTrigger", "TimerWheel"]
