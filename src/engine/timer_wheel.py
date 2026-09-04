"""Timer-wheel boundary for dynamic QUIET_DEADLINE scheduling.

The in-memory queue is intentionally deferred to Phase 5. It must enqueue a
command for DbWriter rather than opening its own SQLite connection.
"""

__all__: list[str] = []
