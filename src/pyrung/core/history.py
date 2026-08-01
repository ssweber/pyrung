"""Historical SystemState query facade for PLC debug APIs.

``History`` is a stateless facade over one PLC's retained execution branch.
Each execution epoch owns its byte-bounded recent-state cache
(``_recent_state_cache``, an ``OrderedDict[int, tuple[SystemState, int]]``
keyed by scan_id), scan log, checkpoints, and synthesis overlay. Recent states
are cache hits; older states are reconstructed on demand by the frozen epoch
that actually executed them. Cache residency is therefore an optimization,
not historical authority.

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
    from pyrung.core.analysis.causal.models import Transition
    from pyrung.core.runner import PLC
    from pyrung.core.state import SystemState

_ANY_TRANSITION_VALUE = object()


class _CausalHistoryWindow:
    """A bounded view onto one runner's inherited causal lineage.

    ``first_transition_scan`` is inclusive.  The immediately preceding state
    remains visible as boundary context, but is index zero in ``scan_ids()``
    and therefore cannot itself become a transition inside the window.

    The backing :class:`History` still resolves every state through its owning
    execution epoch.  This view narrows a causal question; it does not copy,
    replay, or reinterpret any part of the lineage.
    """

    def __init__(
        self,
        backing: History,
        first_transition_scan: int,
        last_scan: int | None,
    ) -> None:
        newest = (
            backing.newest_scan_id if last_scan is None else min(last_scan, backing.newest_scan_id)
        )
        context = max(backing.oldest_scan_id, first_transition_scan - 1)
        self._backing = backing
        self._oldest = context
        self._newest = newest

    def at(self, scan_id: int) -> SystemState:
        if scan_id < self._oldest or scan_id > self._newest:
            raise KeyError(scan_id)
        return self._backing.at(scan_id)

    def range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        return self._backing.range(
            max(self._oldest, start_scan_id),
            min(self._newest + 1, end_scan_id),
        )

    @property
    def oldest_scan_id(self) -> int:
        return self._oldest

    @property
    def newest_scan_id(self) -> int:
        return self._newest

    def scan_ids(self) -> Sequence[int]:
        if self._newest < self._oldest:
            return range(0)
        return range(self._oldest, self._newest + 1)

    def _committed_transition_at(self, tag_name: str, scan_id: int) -> Transition | None:
        if scan_id <= self._oldest or scan_id > self._newest:
            return None
        return self._backing._committed_transition_at(tag_name, scan_id)

    def _last_committed_transition_before(
        self,
        tag_name: str,
        before_scan_id: int,
    ) -> Transition | None:
        transition = self._backing._last_committed_transition_before(
            tag_name,
            min(before_scan_id, self._newest + 1),
        )
        return transition if transition is not None and transition.scan_id > self._oldest else None


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

    Backed by the retained branch's immutable execution epochs. Each epoch
    serves recent scans from its local cache and reconstructs older scans from
    its own scan log and checkpoints under its original overlay.
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

        Recent scans in the owning execution epoch's byte-bounded cache and
        scan-log checkpoints return immutable snapshots. Older scans are
        reconstructed by that same epoch from its nearest checkpoint and scan
        log; a later fork's overlay never reinterprets the inherited scan.

        Raises:
            KeyError: ``scan_id`` falls outside the addressable range
            ``[history.oldest_scan_id, plc._state.scan_id]`` on the retained
            inherited branch.
        """
        if not isinstance(scan_id, int):
            raise KeyError(scan_id)
        return self._plc._causal_state_at(scan_id)

    def range(self, start_scan_id: int, end_scan_id: int) -> list[SystemState]:
        """Return states where ``start <= scan_id < end`` (oldest -> newest)."""
        if end_scan_id <= start_scan_id:
            return []
        tip = self._plc._state.scan_id
        lo = max(self.oldest_scan_id, start_scan_id)
        hi = min(tip, end_scan_id - 1)
        if lo > hi:
            return []
        return self._plc._causal_history_range(lo, hi + 1)

    def latest(self, n: int) -> list[SystemState]:
        """Return up to the latest ``n`` states (oldest -> newest)."""
        if n <= 0:
            return []
        tip = self._plc._state.scan_id
        oldest_target = max(self.oldest_scan_id, tip - n + 1)
        return self.range(oldest_target, tip + 1)

    @property
    def oldest_scan_id(self) -> int:
        """Oldest retained scan on this runner's inherited branch."""
        return self._plc._causal_oldest_scan_id()

    @property
    def newest_scan_id(self) -> int:
        """Newest addressable scan id (current tip)."""
        return self._plc._state.scan_id

    def contains(self, scan_id: int) -> bool:
        """Return True if ``scan_id`` is addressable."""
        if not isinstance(scan_id, int):
            return False
        return self._plc._causal_history_contains(scan_id)

    def _causal_window(
        self,
        first_transition_scan: int,
        last_scan: int | None = None,
    ) -> _CausalHistoryWindow:
        """Return a no-copy bounded view for an incident-local cause query."""
        return _CausalHistoryWindow(self, first_transition_scan, last_scan)

    def scan_ids(self) -> Sequence[int]:
        """Return the addressable scan ids as a ``range`` (oldest -> newest)."""
        return range(self.oldest_scan_id, self._plc._state.scan_id + 1)

    def _committed_transition_at(self, tag_name: str, scan_id: int) -> Transition | None:
        """Authoritative committed boundary from the retained epoch index."""
        return self._plc._causal_committed_transition_at(tag_name, scan_id)

    def _last_committed_transition_before(
        self,
        tag_name: str,
        before_scan_id: int,
    ) -> Transition | None:
        """Latest indexed committed boundary across the inherited lineage."""
        return self._plc._causal_last_committed_transition_before(tag_name, before_scan_id)

    def previous_transition(
        self,
        tag: Any,
        *,
        to: Any = _ANY_TRANSITION_VALUE,
        at_or_before: int | None = None,
    ) -> Transition | None:
        """Return the latest matching transition at or before a scan.

        The query is independent of the history encoder's representation:
        stable, alternating, and arithmetic firing ranges jump to compressed
        candidates; input changes use the scan log; opaque ranges fall back
        conservatively. ``to`` may be any tag value, including ``None``.
        """
        from pyrung.core.analysis.causal.history import (
            _find_last_transition_scan,
            _find_transition_at_scan,
        )

        name = getattr(tag, "name", tag)
        if not isinstance(name, str):
            raise TypeError("tag must be a Tag or tag-name string")
        newest = self.newest_scan_id
        if at_or_before is None:
            cursor = newest + 1
        else:
            if not isinstance(at_or_before, int):
                raise TypeError("at_or_before must be an int or None")
            cursor = min(at_or_before, newest) + 1
        oldest = self.oldest_scan_id
        if cursor <= oldest:
            return None

        plc = self._plc
        pdg = plc._ensure_pdg() if plc._logic else None
        initial_tags = plc._causal_initial_tags
        while cursor > oldest:
            scan_id = _find_last_transition_scan(
                self,
                name,
                cursor,
                timelines=plc._causal_rung_firing_timelines,
                pdg=pdg,
                scan_log=plc._scan_log,
                initial_tags=initial_tags,
            )
            if scan_id is None:
                return None
            transition = _find_transition_at_scan(
                self,
                name,
                scan_id,
                timelines=plc._causal_rung_firing_timelines,
                pdg=pdg,
                scan_log=plc._scan_log,
                initial_tags=initial_tags,
            )
            if transition is not None and (
                to is _ANY_TRANSITION_VALUE or transition.to_value == to
            ):
                return transition
            cursor = scan_id
        return None

    def at_or_before_timestamp(self, timestamp: float) -> SystemState | None:
        """Return the latest state with ``state.timestamp <= timestamp``.

        FIXED_STEP: ``scan_id = floor(timestamp / dt)``, clamped to the
        addressable range.  REALTIME: walks the recent-state cache for
        in-range targets, otherwise walks the ``ScanLog`` ``dts`` array
        cumulatively to locate the scan.  REALTIME lookups outside the
        cache are O(N) in the number of recorded dts.
        """
        from pyrung.core.time_mode import TimeMode

        tip = self._plc._state.scan_id
        oldest = self.oldest_scan_id

        if self._plc._time_mode == TimeMode.FIXED_STEP:
            dt = self._plc._dt
            if dt <= 0:
                return None
            target = int(timestamp / dt)
            if target < oldest:
                # No scan satisfies ``timestamp(scan) <= target_ts``;
                # caller (e.g. ``rewind``) will fall back to oldest.
                oldest_state = self.at(oldest)
                return oldest_state if oldest_state.timestamp <= timestamp else None
            target = min(target, tip)
            return self.at(target)

        # REALTIME: prefer the cache when the target falls inside it.
        cache = self._plc._recent_state_cache
        if cache:
            first_state = next(iter(cache.values()))[0]
            if timestamp >= first_state.timestamp:
                best: SystemState | None = None
                for _, (state, _) in cache.items():
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
