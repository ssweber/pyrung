"""Historical SystemState query facade for PLC debug APIs.

Stage 5 of the record-and-replay migration replaced the per-scan
deque/dict snapshot store with a thin facade over the PLC's
``_recent_state_window`` (~20 most recent committed scans, kept live
for monitor ``previous_value`` and ``_prev:*`` edge detection) and
``replay_to`` (reconstructs older scans on demand from the
``ScanLog`` plus checkpoints).

This class no longer holds ``SystemState`` objects directly except
through its back-reference to the owning ``PLC``.  Labels remain on
``History`` as a pure overlay, decoupled from state storage — any
``scan_id`` from ``0`` up to the current tip is a valid label target.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.runner import PLC
    from pyrung.core.state import SystemState


@dataclass(frozen=True)
class LabeledSnapshot:
    """Label metadata attached to one labeled scan."""

    label: str
    scan_id: int
    timestamp: float
    rtc_iso: str | None = None
    rtc_offset_seconds: float | None = None


class History:
    """Read-only query surface for historical ``SystemState``.

    Backed by the owning PLC's ``_recent_state_window`` (cheap, recent
    scans) and ``replay_to`` (older scans, reconstructed on demand).
    """

    def __init__(self, plc: PLC) -> None:
        self._plc = plc
        self._label_to_scan_ids: dict[str, deque[int]] = {}
        self._scan_id_to_labels: dict[int, set[str]] = {}
        self._label_scan_metadata: dict[tuple[str, int], dict[str, Any]] = {}

    # ------------------------------------------------------------------
    # State queries
    # ------------------------------------------------------------------

    def at(self, scan_id: int) -> SystemState:
        """Return the ``SystemState`` for ``scan_id``.

        Recent scans (within the PLC's ``_recent_state_window``) and
        scan-log checkpoints return live snapshots.  Older scans are
        reconstructed via ``plc.replay_to(scan_id).current_state``;
        each reconstruction forks from the nearest checkpoint and
        walks the scan log forward.

        Raises:
            KeyError: ``scan_id`` falls outside the addressable range
            ``[plc._initial_scan_id, plc._state.scan_id]``.
        """
        if not isinstance(scan_id, int):
            raise KeyError(scan_id)
        return self._plc._state_at(scan_id)

    def range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        """Return states where ``start <= scan_id < end`` (oldest -> newest)."""
        if end_scan_id <= start_scan_id:
            return []
        tip = self._plc._state.scan_id
        lo = max(self._plc._initial_scan_id, start_scan_id)
        hi = min(tip, end_scan_id - 1)
        if lo > hi:
            return []

        window = self._plc._recent_state_window
        window_lo = window[0].scan_id if window else tip + 1
        if lo >= window_lo:
            return [s for s in window if lo <= s.scan_id <= hi]
        if hi < window_lo:
            return self._plc._replay_range(lo, hi)
        # Range straddles the window boundary: replay the older slice,
        # serve the rest from the window.
        replayed = self._plc._replay_range(lo, window_lo - 1)
        windowed = [s for s in window if window_lo <= s.scan_id <= hi]
        return replayed + windowed

    def latest(self, n: int) -> list[SystemState]:
        """Return up to the latest ``n`` states (oldest -> newest)."""
        if n <= 0:
            return []
        tip = self._plc._state.scan_id
        oldest_target = max(self._plc._initial_scan_id, tip - n + 1)
        return self.range(oldest_target, tip + 1)

    @property
    def oldest_scan_id(self) -> int:
        """Oldest addressable scan id (the PLC's initial scan_id)."""
        return self._plc._initial_scan_id

    @property
    def newest_scan_id(self) -> int:
        """Newest addressable scan id (current tip)."""
        return self._plc._state.scan_id

    def contains(self, scan_id: int) -> bool:
        """Return True if ``scan_id`` is addressable."""
        if not isinstance(scan_id, int):
            return False
        return self._plc._initial_scan_id <= scan_id <= self._plc._state.scan_id

    def scan_ids(self) -> Sequence[int]:
        """Return the addressable scan ids as a ``range`` (oldest -> newest)."""
        return range(self._plc._initial_scan_id, self._plc._state.scan_id + 1)

    def at_or_before_timestamp(self, timestamp: float) -> SystemState | None:
        """Return the latest state with ``state.timestamp <= timestamp``.

        FIXED_STEP: ``scan_id = floor(timestamp / dt)``, clamped to the
        addressable range.  REALTIME: walks the recent-state window for
        in-range targets, otherwise walks the ``ScanLog`` ``dts`` array
        cumulatively to locate the scan.  REALTIME lookups outside the
        window are O(N) in the number of recorded dts.
        """
        from pyrung.core.time_mode import TimeMode

        tip = self._plc._state.scan_id
        oldest = self._plc._initial_scan_id

        if self._plc._time_mode == TimeMode.FIXED_STEP:
            dt = self._plc._dt
            if dt <= 0:
                return None
            target = int(timestamp / dt)
            if target < oldest:
                # No scan satisfies ``timestamp(scan) <= target_ts``;
                # caller (e.g. ``rewind``) will fall back to oldest.
                oldest_state = self._plc._state_at(oldest)
                return oldest_state if oldest_state.timestamp <= timestamp else None
            target = min(target, tip)
            return self.at(target)

        # REALTIME: prefer the live window when the target falls inside it.
        window = self._plc._recent_state_window
        if window and timestamp >= window[0].timestamp:
            best: SystemState | None = None
            for state in window:
                if state.timestamp <= timestamp:
                    best = state
                else:
                    break
            return best

        # Older targets: walk dts cumulatively.  ``timestamp(scan_id) ==
        # sum(dts[:scan_id])``; find the largest scan_id with that
        # accumulated sum <= target.
        log = self._plc._scan_log
        dts = log._dts
        if dts is None or len(dts) == 0:
            # Initial state has timestamp 0; if target is non-negative
            # return it, else None.
            return self.at(0) if timestamp >= 0 else None

        accumulated = 0.0
        last_scan = 0
        # dts[i] is the dt that produced scan (base_scan + i + 1).
        for i, dt_value in enumerate(dts):
            next_scan = log.base_scan + i + 1
            next_timestamp = accumulated + dt_value
            if next_timestamp > timestamp:
                break
            accumulated = next_timestamp
            last_scan = next_scan
        if last_scan > tip:
            last_scan = tip
        return self.at(last_scan) if last_scan >= 0 else None

    # ------------------------------------------------------------------
    # Labels (overlay; not tied to state storage)
    # ------------------------------------------------------------------

    def find(self, label: str) -> SystemState | None:
        """Return the most recent labeled state, or None."""
        scan_ids = self._label_to_scan_ids.get(label)
        if not scan_ids:
            return None
        return self.at(scan_ids[-1])

    def find_all(self, label: str) -> list[SystemState]:
        """Return all states for ``label`` (oldest -> newest)."""
        scan_ids = self._label_to_scan_ids.get(label)
        if not scan_ids:
            return []
        return [self.at(scan_id) for scan_id in scan_ids]

    def find_labeled(self, label: str) -> LabeledSnapshot | None:
        """Return the most recent labeled snapshot with metadata."""
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

    # ------------------------------------------------------------------
    # Internal hooks called from runner
    # ------------------------------------------------------------------

    def _label_scan(
        self, label: str, scan_id: int, *, metadata: dict[str, Any] | None = None
    ) -> None:
        """Attach ``label`` to ``scan_id``; deduplicated per (label, scan_id).

        Any addressable scan_id (``0 <= scan_id <= tip``) is valid.
        Future log-trim work will sweep labels whose scan_id falls below
        the earliest reconstructable scan.
        """
        if not self.contains(scan_id):
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

    def _reset_labels(self) -> None:
        """Drop all label state.  Called from lifecycle resets."""
        self._label_to_scan_ids.clear()
        self._scan_id_to_labels.clear()
        self._label_scan_metadata.clear()

    def _labeled_snapshot(self, *, label: str, scan_id: int) -> LabeledSnapshot:
        state = self.at(scan_id)
        metadata = self._label_scan_metadata.get((label, scan_id), {})
        rtc_iso = metadata.get("rtc_iso")
        rtc_offset_seconds = metadata.get("rtc_offset_seconds")
        return LabeledSnapshot(
            label=label,
            scan_id=scan_id,
            timestamp=state.timestamp,
            rtc_iso=rtc_iso if isinstance(rtc_iso, str) else None,
            rtc_offset_seconds=(
                float(rtc_offset_seconds) if isinstance(rtc_offset_seconds, int | float) else None
            ),
        )
