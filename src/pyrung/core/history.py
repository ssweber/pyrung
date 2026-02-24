"""Historical SystemState storage for PLCRunner debug APIs."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from pyrung.core.state import SystemState


@dataclass(frozen=True)
class LabeledSnapshot:
    """Label metadata attached to one retained scan snapshot."""

    label: str
    scan_id: int
    timestamp: float
    rtc_iso: str | None = None
    rtc_offset_seconds: float | None = None


class History:
    """Stores retained scan snapshots with optional oldest-first eviction."""

    def __init__(self, initial_state: SystemState, *, limit: int | None = None) -> None:
        if limit is not None and limit < 1:
            raise ValueError("history_limit must be >= 1 or None")

        self._limit = limit
        self._order: deque[int] = deque([initial_state.scan_id])
        self._by_scan_id: dict[int, SystemState] = {initial_state.scan_id: initial_state}
        self._label_to_scan_ids: dict[str, deque[int]] = {}
        self._scan_id_to_labels: dict[int, set[str]] = {}
        self._label_scan_metadata: dict[tuple[str, int], dict[str, Any]] = {}

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

    def find(self, label: str) -> SystemState | None:
        """Return the most recent retained snapshot labeled ``label``."""
        scan_ids = self._label_to_scan_ids.get(label)
        if not scan_ids:
            return None
        return self._by_scan_id[scan_ids[-1]]

    def find_all(self, label: str) -> list[SystemState]:
        """Return all retained snapshots labeled ``label`` (oldest -> newest)."""
        scan_ids = self._label_to_scan_ids.get(label)
        if not scan_ids:
            return []
        return [self._by_scan_id[scan_id] for scan_id in scan_ids]

    def find_labeled(self, label: str) -> LabeledSnapshot | None:
        """Return the most recent labeled snapshot with attached metadata."""
        scan_ids = self._label_to_scan_ids.get(label)
        if not scan_ids:
            return None
        return self._labeled_snapshot(label=label, scan_id=scan_ids[-1])

    def find_all_labeled(self, label: str) -> list[LabeledSnapshot]:
        """Return all labeled snapshots with metadata (oldest -> newest)."""
        scan_ids = self._label_to_scan_ids.get(label)
        if not scan_ids:
            return []
        return [self._labeled_snapshot(label=label, scan_id=scan_id) for scan_id in scan_ids]

    @property
    def oldest_scan_id(self) -> int:
        """Oldest retained scan id."""
        return self._order[0]

    @property
    def newest_scan_id(self) -> int:
        """Newest retained scan id."""
        return self._order[-1]

    def contains(self, scan_id: int) -> bool:
        """Return True if scan id is currently retained."""
        return scan_id in self._by_scan_id

    def at_or_before_timestamp(self, timestamp: float) -> SystemState | None:
        """Return newest retained snapshot with state.timestamp <= timestamp."""
        for scan_id in reversed(self._order):
            state = self._by_scan_id[scan_id]
            if state.timestamp <= timestamp:
                return state
        return None

    def _append(self, state: SystemState) -> list[int]:
        """Append a newly committed state; for runner-internal use only."""
        scan_id = state.scan_id
        if self._order and scan_id <= self._order[-1]:
            raise ValueError(
                f"scan_id must be strictly increasing; got {scan_id} after {self._order[-1]}"
            )

        self._order.append(scan_id)
        self._by_scan_id[scan_id] = state
        return self._evict_if_needed()

    def _label_scan(
        self, label: str, scan_id: int, *, metadata: dict[str, Any] | None = None
    ) -> None:
        """Attach ``label`` to one retained scan; deduplicated per scan."""
        if scan_id not in self._by_scan_id:
            raise KeyError(scan_id)

        labels = self._scan_id_to_labels.setdefault(scan_id, set())
        if label in labels:
            if metadata is not None:
                self._label_scan_metadata[(label, scan_id)] = dict(metadata)
            return

        labels.add(label)
        self._label_to_scan_ids.setdefault(label, deque()).append(scan_id)
        if metadata is not None:
            self._label_scan_metadata[(label, scan_id)] = dict(metadata)

    def _evict_if_needed(self) -> list[int]:
        evicted_scan_ids: list[int] = []
        if self._limit is None:
            return evicted_scan_ids

        while len(self._order) > self._limit:
            oldest_scan_id = self._order.popleft()
            del self._by_scan_id[oldest_scan_id]
            self._drop_labels_for_scan(oldest_scan_id)
            evicted_scan_ids.append(oldest_scan_id)

        return evicted_scan_ids

    def _drop_labels_for_scan(self, scan_id: int) -> None:
        labels = self._scan_id_to_labels.pop(scan_id, None)
        if labels is None:
            return

        for label in labels:
            self._label_scan_metadata.pop((label, scan_id), None)
            scan_ids = self._label_to_scan_ids.get(label)
            if scan_ids is None:
                continue
            try:
                scan_ids.remove(scan_id)
            except ValueError:
                continue
            if not scan_ids:
                del self._label_to_scan_ids[label]

    def _labeled_snapshot(self, *, label: str, scan_id: int) -> LabeledSnapshot:
        state = self._by_scan_id[scan_id]
        metadata = self._label_scan_metadata.get((label, scan_id), {})
        rtc_iso = metadata.get("rtc_iso")
        rtc_offset_seconds = metadata.get("rtc_offset_seconds")
        return LabeledSnapshot(
            label=label,
            scan_id=scan_id,
            timestamp=state.timestamp,
            rtc_iso=rtc_iso if isinstance(rtc_iso, str) else None,
            rtc_offset_seconds=(
                float(rtc_offset_seconds)
                if isinstance(rtc_offset_seconds, int | float)
                else None
            ),
        )
