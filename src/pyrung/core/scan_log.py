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
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pyrung.core.time_mode import TimeMode


LifecycleKind = Literal["stop", "reboot", "battery_present", "clear_forces"]


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

    def record_force_changes(self, scan_id: int, forces: Mapping[str, Any]) -> None:
        """Record the full force map as it stood for ``scan_id``.

        Called only when the force map has changed since the prior
        record, or at checkpoint scans (where the replay invariant
        requires an unconditional write — enforced by the caller).
        """
        self._force_changes_by_scan[scan_id] = dict(forces)

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

    def snapshot(self) -> ScanLogSnapshot:
        """Return a frozen view of the log, safe to outlive further writes."""
        return ScanLogSnapshot(
            base_scan=self._base_scan,
            patches_by_scan={k: dict(v) for k, v in self._patches_by_scan.items()},
            force_changes_by_scan={k: dict(v) for k, v in self._force_changes_by_scan.items()},
            rtc_base_changes=dict(self._rtc_base_changes),
            dts=array.array("d", self._dts) if self._dts is not None else None,
            lifecycle_events=tuple(self._lifecycle_events),
        )

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
        for d in (self._patches_by_scan, self._force_changes_by_scan, self._rtc_base_changes):
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
        return size
