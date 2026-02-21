"""Historical SystemState storage for PLCRunner debug APIs."""

from __future__ import annotations

from collections import deque

from pyrung.core.state import SystemState


class History:
    """Stores retained scan snapshots with optional oldest-first eviction."""

    def __init__(self, initial_state: SystemState, *, limit: int | None = None) -> None:
        if limit is not None and limit < 1:
            raise ValueError("history_limit must be >= 1 or None")

        self._limit = limit
        self._order: deque[int] = deque([initial_state.scan_id])
        self._by_scan_id: dict[int, SystemState] = {initial_state.scan_id: initial_state}

    def at(self, scan_id: int) -> SystemState:
        """Return snapshot for a retained scan id."""
        try:
            return self._by_scan_id[scan_id]
        except KeyError as exc:
            raise KeyError(scan_id) from exc

    def range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        """Return retained snapshots where start <= scan_id < end."""
        if end_scan_id <= start_scan_id:
            return []
        return [
            self._by_scan_id[scan_id]
            for scan_id in self._order
            if start_scan_id <= scan_id < end_scan_id
        ]

    def latest(self, n: int) -> list[SystemState]:
        """Return up to the latest n retained snapshots (oldest -> newest)."""
        if n <= 0:
            return []

        retained = list(self._order)
        tail_ids = retained[-n:]
        return [self._by_scan_id[scan_id] for scan_id in tail_ids]

    def _append(self, state: SystemState) -> None:
        """Append a newly committed state; for runner-internal use only."""
        scan_id = state.scan_id
        if self._order and scan_id <= self._order[-1]:
            raise ValueError(
                f"scan_id must be strictly increasing; got {scan_id} after {self._order[-1]}"
            )

        self._order.append(scan_id)
        self._by_scan_id[scan_id] = state
        self._evict_if_needed()

    def _evict_if_needed(self) -> None:
        if self._limit is None:
            return

        while len(self._order) > self._limit:
            oldest_scan_id = self._order.popleft()
            del self._by_scan_id[oldest_scan_id]
