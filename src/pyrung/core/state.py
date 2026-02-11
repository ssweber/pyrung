"""Immutable system state for PLC simulation.

The core data structure for the Redux-style architecture.
All state transitions produce new SystemState instances.
"""

from __future__ import annotations

from typing import Any, cast

from pyrsistent import PMap, PRecord, field, pmap


class SystemState(PRecord):
    """Immutable snapshot of PLC state at a point in time.

    Attributes:
        scan_id: Monotonically increasing scan counter.
        timestamp: Simulation clock in seconds.
        tags: Immutable mapping of tag names to values (bool, int, float, str).
        memory: Immutable mapping for internal state (timers, counters, etc).
    """

    scan_id = field(type=int, initial=0)
    timestamp = field(type=float, initial=0.0)
    tags = field(type=PMap, initial=pmap())
    memory = field(type=PMap, initial=pmap())

    def with_tags(self, updates: dict[str, bool | int | float | str]) -> SystemState:
        """Return new state with updated tags. Original unchanged."""
        return self.set(tags=self.tags.update(updates))

    def with_memory(self, updates: dict[str, Any]) -> SystemState:
        """Return new state with updated memory. Original unchanged."""
        return self.set(memory=self.memory.update(updates))

    def next_scan(self, dt: float) -> SystemState:
        """Return new state for next scan cycle.

        Args:
            dt: Time delta in seconds to add to timestamp.
        """
        e = self.evolver()
        e.set("scan_id", self.scan_id + 1)
        e.set("timestamp", self.timestamp + dt)
        return cast(SystemState, e.persistent())
