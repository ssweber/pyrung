"""Measure replay-capture reuse on the real BurnerLoop drive.

This wraps ``PLC._replay_capture_at`` without changing its behavior, records
the requested historical scans, and simulates bounded LRU caches offline.

Run:
    uv run python -u -m scratchpad.burner.probe_replay_capture_cache 40000 180
"""

from __future__ import annotations

from collections import OrderedDict, defaultdict
from dataclasses import dataclass, field
from typing import Any

from pyrung.core.runner import PLC
from scratchpad.burner.drive_y_burnerloop import main


@dataclass
class ReplayCaptureProbe:
    """Replay-capture requests grouped by runner and unchanged live tip."""

    requests: list[tuple[int, int, int]] = field(default_factory=list)
    request_causes: list[int | None] = field(default_factory=list)
    owners: dict[PLC, int] = field(default_factory=dict)
    active_causes: list[int] = field(default_factory=list)
    cause_labels: dict[int, str] = field(default_factory=dict)
    one_slot_hits: int = 0

    def record(self, plc: PLC, target_scan: int) -> None:
        owner = self.owners.setdefault(plc, len(self.owners))
        self.requests.append((owner, plc.state.scan_id, target_scan))
        self.request_causes.append(self.active_causes[-1] if self.active_causes else None)
        cached = plc._cached_replay_capture
        if cached is not None and cached[0] == target_scan:
            self.one_slot_hits += 1

    def begin_cause(self, tag: object, scan: int | None, deep: bool) -> None:
        if self.active_causes:
            self.active_causes.append(self.active_causes[-1])
            return
        cause_id = len(self.cause_labels)
        self.cause_labels[cause_id] = f"{tag}@{scan if scan is not None else 'tip'} deep={deep}"
        self.active_causes.append(cause_id)

    def end_cause(self) -> None:
        self.active_causes.pop()

    def simulated_hits(self, capacity: int) -> int:
        caches: dict[tuple[int, int], OrderedDict[int, None]] = {}
        hits = 0
        for owner, live_tip, target_scan in self.requests:
            cache = caches.setdefault((owner, live_tip), OrderedDict())
            if target_scan in cache:
                hits += 1
                cache.move_to_end(target_scan)
                continue
            cache[target_scan] = None
            if len(cache) > capacity:
                cache.popitem(last=False)
        return hits

    def report(self) -> None:
        total = len(self.requests)
        current_misses = total - self.one_slot_hits
        distinct = len(set(self.requests))
        print("\nReplay capture cache probe")
        print(f"  requests: {total:,}")
        print(f"  distinct runner/tip/target keys: {distinct:,}")
        print(f"  observed one-slot hits: {self.one_slot_hits:,}")
        print(f"  observed replay executions: {current_misses:,}")
        for capacity in (1, 2, 4, 8, 16, 32, 64):
            hits = self.simulated_hits(capacity)
            misses = total - hits
            print(
                f"  LRU {capacity:>2}: {hits:>6,} hits, {misses:>6,} executions, "
                f"{current_misses - misses:>6,} avoided"
            )

        by_cause: dict[int, list[tuple[int, int, int]]] = defaultdict(list)
        outside = 0
        for key, cause_id in zip(self.requests, self.request_causes, strict=True):
            if cause_id is None:
                outside += 1
            else:
                by_cause[cause_id].append(key)
        per_cause_distinct = {cause_id: len(set(keys)) for cause_id, keys in by_cause.items()}
        all_cause_keys = {key for keys in by_cause.values() for key in keys}
        summed = sum(per_cause_distinct.values())
        print("\nCause boundaries")
        print(f"  cause() calls with capture requests: {len(by_cause):,}")
        print(f"  requests outside cause(): {outside:,}")
        print(f"  distinct captures summed per cause(): {summed:,}")
        print(f"  distinct captures across all cause(): {len(all_cause_keys):,}")
        print(f"  cross-cause recomputations: {summed - len(all_cause_keys):,}")
        for cause_id, distinct_count in sorted(
            per_cause_distinct.items(), key=lambda item: item[1], reverse=True
        )[:10]:
            print(
                f"    {distinct_count:>5,} distinct / {len(by_cause[cause_id]):>5,} requests  "
                f"{self.cause_labels[cause_id]}"
            )


def run() -> None:
    probe = ReplayCaptureProbe()
    original = PLC._replay_capture_at
    original_cause = PLC.cause

    def wrapped(plc: PLC, target_scan_id: int) -> Any:
        probe.record(plc, target_scan_id)
        return original(plc, target_scan_id)

    def wrapped_cause(
        plc: PLC,
        tag: object,
        scan: int | None = None,
        **kwargs: Any,
    ) -> Any:
        deep = bool(kwargs.get("deep", True))
        probe.begin_cause(tag, scan, deep)
        try:
            return original_cause(plc, tag, scan=scan, **kwargs)
        finally:
            probe.end_cause()

    PLC._replay_capture_at = wrapped  # ty: ignore[invalid-assignment]
    PLC.cause = wrapped_cause  # ty: ignore[invalid-assignment]
    try:
        main()
    finally:
        PLC._replay_capture_at = original  # ty: ignore[invalid-assignment]
        PLC.cause = original_cause  # ty: ignore[invalid-assignment]
        probe.report()


if __name__ == "__main__":
    run()
