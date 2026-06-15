"""Sparse-by-field capture of scan nondeterminism for replay.

The replay architecture reconstructs any historical ``SystemState`` by
forking from a checkpoint and re-running scans forward.  Re-running is
deterministic given ``(state, dt, patches, forces, rtc_base)``; only
those channels need to be recorded.

``ScanLog`` stores them as sparse side-structures keyed by ``scan_id``:

- ``patches_by_scan`` — scans where a ``plc.patch()`` was drained.
- ``force_changes_by_scan`` — scans where the force map changed from
  its prior state.  Checkpoints additionally write a full snapshot
  here (replay correctness invariant, enforced at the checkpoint
  write site — not here).
- ``rtc_base_changes`` — scans where ``_set_rtc_internal`` was called.
- ``dts`` — dense per-scan ``dt`` values, populated only in REALTIME
  mode (in FIXED_STEP mode the PLC's constant ``_dt`` is authoritative
  and replay reads it from config).
- ``lifecycle_events`` — ``stop``/``reboot``/``battery_present``/
  ``clear_forces`` operations that happen between scans.

Idle scans contribute **zero bytes**: if nothing happened on scan N,
no key lands in any sparse dict and (in FIXED_STEP) no array slot is
added.  This is the whole point of the sparse-by-field layout.
"""

from __future__ import annotations

import array
import bisect
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pyrung.core.time_mode import TimeMode


LifecycleKind = Literal["stop", "reboot", "battery_present", "clear_forces"]


@dataclass(frozen=True)
class IoSubmitRecord:
    """Recorded tag writes for an I/O submit (send/receive start)."""

    tag_writes: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class IoResultRecord:
    """Recorded result of an I/O drain (send/receive completion)."""

    ok: bool
    exception_code: int
    values: tuple[Any, ...]
    tag_writes: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class LifecycleEvent:
    """A lifecycle operation between scans.

    ``at_scan_id`` is the would-be-next scan_id at the time of the
    event — i.e., ``state.scan_id + 1`` when the event fired.  If that
    scan never executes, the event is vestigial for replay purposes
    (but kept as a timeline record).

    ``value`` is used only by ``battery_present`` (True/False).  For
    other kinds it is ``None``.
    """

    at_sim_time: float
    at_scan_id: int
    kind: LifecycleKind
    value: bool | None = None


@dataclass(frozen=True)
class ScanLogSnapshot:
    """Atomic frozen view of a ``ScanLog`` for a replay consumer.

    Returned by ``ScanLog.snapshot()``.  The ``dts`` ``array.array`` is
    a deep copy — the live log may append to or trim its underlying
    array while the snapshot is in use, and a bare reference would
    produce stale or crashing reads.  The sparse dicts are shallow
    copies; their inner values are immutable (patches were drained,
    forces are rebuilt per mutation, RTC tuples are frozen).
    """

    base_scan: int
    patches_by_scan: Mapping[int, Mapping[str, Any]]
    force_changes_by_scan: Mapping[int, Mapping[str, Any]]
    rtc_base_changes: Mapping[int, tuple[datetime, float]]
    dts: array.array | None
    lifecycle_events: tuple[LifecycleEvent, ...]
    io_submits_by_scan: Mapping[int, Mapping[str, IoSubmitRecord]]
    io_drains_by_scan: Mapping[int, Mapping[str, IoResultRecord]]


class ScanLog:
    """Live, append-only record of scan nondeterminism."""

    def __init__(self, *, time_mode: TimeMode, base_scan: int = 0) -> None:
        from pyrung.core.time_mode import TimeMode as _TimeMode

        self._base_scan = base_scan
        self._patches_by_scan: dict[int, dict[str, Any]] = {}
        self._force_changes_by_scan: dict[int, dict[str, Any]] = {}
        self._rtc_base_changes: dict[int, tuple[datetime, float]] = {}
        self._dts: array.array[float] | None = (
            array.array("d") if time_mode == _TimeMode.REALTIME else None
        )
        self._lifecycle_events: list[LifecycleEvent] = []
        self._io_submits_by_scan: dict[int, dict[str, IoSubmitRecord]] = {}
        self._io_drains_by_scan: dict[int, dict[str, IoResultRecord]] = {}
        self._effective_input_cache: dict[str, tuple[Any, tuple[tuple[int, Any, Any], ...]]] = {}

    @property
    def base_scan(self) -> int:
        return self._base_scan

    @property
    def records_dt(self) -> bool:
        return self._dts is not None

    def record_patches(self, scan_id: int, patches: Mapping[str, Any]) -> None:
        """Record patches applied on ``scan_id``.  No-op if empty."""
        if patches:
            self._patches_by_scan[scan_id] = dict(patches)
            self._effective_input_cache.clear()

    def record_force_changes(self, scan_id: int, forces: Mapping[str, Any]) -> None:
        """Record the full force map as it stood for ``scan_id``.

        Called only when the force map has changed since the prior
        record, or at checkpoint scans (where the replay invariant
        requires an unconditional write — enforced by the caller).
        """
        self._force_changes_by_scan[scan_id] = dict(forces)
        self._effective_input_cache.clear()

    def record_rtc_base_change(self, scan_id: int, base: datetime, base_sim_time: float) -> None:
        """Record an RTC base update taking effect at ``scan_id``."""
        self._rtc_base_changes[scan_id] = (base, float(base_sim_time))

    def record_dt(self, scan_id: int, dt: float) -> None:
        """Record ``dt`` for ``scan_id`` in REALTIME mode.  No-op in FIXED_STEP."""
        if self._dts is None:
            return
        index = scan_id - self._base_scan
        if index < 0:
            return
        while len(self._dts) <= index:
            self._dts.append(0.0)
        self._dts[index] = float(dt)

    def record_lifecycle(self, event: LifecycleEvent) -> None:
        self._lifecycle_events.append(event)

    def record_io_submit(self, scan_id: int, key: str, record: IoSubmitRecord) -> None:
        self._io_submits_by_scan.setdefault(scan_id, {})[key] = record

    def record_io_drain(self, scan_id: int, key: str, record: IoResultRecord) -> None:
        self._io_drains_by_scan.setdefault(scan_id, {})[key] = record

    def snapshot(self) -> ScanLogSnapshot:
        """Return a frozen view of the log, safe to outlive further writes."""
        return ScanLogSnapshot(
            base_scan=self._base_scan,
            patches_by_scan={k: dict(v) for k, v in self._patches_by_scan.items()},
            force_changes_by_scan={k: dict(v) for k, v in self._force_changes_by_scan.items()},
            rtc_base_changes=dict(self._rtc_base_changes),
            dts=array.array("d", self._dts) if self._dts is not None else None,
            lifecycle_events=tuple(self._lifecycle_events),
            io_submits_by_scan=dict(self._io_submits_by_scan),
            io_drains_by_scan=dict(self._io_drains_by_scan),
        )

    def effective_input_changes(
        self,
        tag_name: str,
        *,
        initial_value: Any,
    ) -> tuple[tuple[int, Any, Any], ...]:
        """Derived effective value changes for an externally supplied tag.

        ``ScanLog`` records input events by scan. Causal lookup wants the
        inverse view: for one writerless input tag, the scans where its
        committed scan-boundary value changed. This method derives that
        view from patches plus force-map snapshots and caches it lazily.

        Returned tuples are ``(scan_id, from_value, to_value)``. Scan ids
        at or before ``base_scan`` are treated as already folded into
        *initial_value*.
        """
        cached = self._effective_input_cache.get(tag_name)
        if cached is not None and cached[0] == initial_value:
            return cached[1]

        base_scan = self._base_scan
        current = initial_value
        force_active = False
        force_value: Any = None

        for scan_id in sorted(self._force_changes_by_scan):
            if scan_id > base_scan:
                break
            forces = self._force_changes_by_scan[scan_id]
            force_active = tag_name in forces
            force_value = forces.get(tag_name)

        event_scans = {
            scan_id
            for scan_id, patches in self._patches_by_scan.items()
            if scan_id > base_scan and tag_name in patches
        }
        event_scans.update(
            scan_id for scan_id in self._force_changes_by_scan if scan_id > base_scan
        )

        changes: list[tuple[int, Any, Any]] = []
        for scan_id in sorted(event_scans):
            forces = self._force_changes_by_scan.get(scan_id)
            if forces is not None:
                force_active = tag_name in forces
                force_value = forces.get(tag_name)

            patches = self._patches_by_scan.get(scan_id)
            if force_active:
                next_value = force_value
            elif patches is not None and tag_name in patches:
                next_value = patches[tag_name]
            else:
                next_value = current

            if next_value != current:
                changes.append((scan_id, current, next_value))
                current = next_value

        frozen = tuple(changes)
        self._effective_input_cache[tag_name] = (initial_value, frozen)
        return frozen

    def effective_input_transition_at(
        self,
        tag_name: str,
        scan_id: int,
        *,
        initial_value: Any,
    ) -> tuple[int, Any, Any] | None:
        """Return this input tag's effective transition at exactly *scan_id*."""
        changes = self.effective_input_changes(tag_name, initial_value=initial_value)
        idx = bisect.bisect_left(changes, scan_id, key=lambda item: item[0])
        if idx < len(changes) and changes[idx][0] == scan_id:
            return changes[idx]
        return None

    def latest_effective_input_transition(
        self,
        tag_name: str,
        *,
        initial_value: Any,
    ) -> tuple[int, Any, Any] | None:
        """Return this input tag's latest effective transition, if any."""
        changes = self.effective_input_changes(tag_name, initial_value=initial_value)
        return changes[-1] if changes else None

    def last_effective_input_change_before(
        self,
        tag_name: str,
        before_scan_id: int,
        *,
        initial_value: Any,
    ) -> int | None:
        """Return this input tag's latest change scan before *before_scan_id*."""
        changes = self.effective_input_changes(tag_name, initial_value=initial_value)
        idx = bisect.bisect_left(changes, before_scan_id, key=lambda item: item[0])
        if idx > 0:
            return changes[idx - 1][0]
        return None

    def effective_input_value_at(
        self,
        tag_name: str,
        scan_id: int,
        *,
        initial_value: Any,
    ) -> Any:
        """Return this input tag's effective scan-boundary value at *scan_id*."""
        changes = self.effective_input_changes(tag_name, initial_value=initial_value)
        idx = bisect.bisect_right(changes, scan_id, key=lambda item: item[0]) - 1
        if idx >= 0:
            return changes[idx][2]
        return initial_value

    def trim_before(self, scan_id: int) -> None:
        """Advance the replay horizon: drop all log entries for scans < scan_id.

        After this call, replay_to(k) for k < scan_id is unsupported —
        the inputs needed to reconstruct those scans are gone.  The
        caller is responsible for trimming checkpoints in lockstep so
        that at least one anchor at or after scan_id survives.

        No-op if scan_id <= base_scan (nothing to drop).
        """
        if scan_id <= self._base_scan:
            return
        for d in (
            self._patches_by_scan,
            self._force_changes_by_scan,
            self._rtc_base_changes,
            self._io_submits_by_scan,
            self._io_drains_by_scan,
        ):
            for k in [k for k in d if k < scan_id]:
                del d[k]
        if self._dts is not None:
            drop = scan_id - self._base_scan
            if drop > 0:
                del self._dts[:drop]
            self._base_scan = scan_id
        else:
            self._base_scan = scan_id
        self._lifecycle_events = [e for e in self._lifecycle_events if e.at_scan_id >= scan_id]
        self._effective_input_cache.clear()

    def bytes_estimate(self) -> int:
        """Rough memory estimate for tests and benchmarking.

        Undercount vs. real Python overhead, but stable under the
        sparse-by-field property — idle scans return ~0 bytes.
        """
        size = 0
        for patches in self._patches_by_scan.values():
            size += 80 + 40 * len(patches)
        for forces in self._force_changes_by_scan.values():
            size += 80 + 40 * len(forces)
        size += 48 * len(self._rtc_base_changes)
        if self._dts is not None:
            size += 8 * len(self._dts)
        size += 48 * len(self._lifecycle_events)
        for submits in self._io_submits_by_scan.values():
            size += 80 + 80 * len(submits)
        for drains in self._io_drains_by_scan.values():
            size += 80 + 120 * len(drains)
        for _initial_value, changes in self._effective_input_cache.values():
            size += 80 + 48 * len(changes)
        return size
