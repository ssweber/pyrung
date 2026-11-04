"""Immutable system state for PLC simulation.

The core data structure for the Redux-style architecture.
All state transitions produce new SystemState instances.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from pyrsistent import PMap, PRecord, field, pmap


def snapshot_dict(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Plain-dict copy of ``mapping``, fast for a ``PMap``.

    ``dict(pmap)`` sees a non-dict mapping and calls ``keys()`` plus a Python
    ``__getitem__`` per key; ``iteritems()`` walks the buckets directly (about
    6x faster on a few thousand keys).  Any other mapping takes ``dict()``.
    """
    if isinstance(mapping, PMap):
        return dict(mapping.iteritems())
    return dict(mapping)


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

    def with_tags(self, updates: Mapping[str, bool | int | float | str]) -> SystemState:
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

    def committed(self, tags: PMap, memory: PMap, dt: float) -> SystemState:
        """Return the state after a scan: these tags and memory, on the next scan."""
        return self.scanned(self.scan_id + 1, self.timestamp + dt, tags, memory)

    def scanned(self, scan_id: int, timestamp: float, tags: PMap, memory: PMap) -> SystemState:
        """Return this state with every field replaced, cheaper than building a new one."""
        e = self.evolver()
        e.set("scan_id", scan_id)
        e.set("timestamp", timestamp)
        e.set("tags", tags)
        e.set("memory", memory)
        return cast(SystemState, e.persistent())
