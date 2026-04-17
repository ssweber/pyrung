"""Per-rung range-encoded firing timelines.

Replaces a ``scan_id -> PMap[rung_index, PMap]`` storage that paid one
dict entry per scan for every firing rung.  Rungs that fire the same
pattern for thousands of scans now cost one range; a period-2
alternator (scan-clock toggle) collapses into a single
``AlternatingRun``.

Three payload flavors live in the same timeline:

- :class:`PatternRef` — the rung fired the same canonical write set
  for every scan in ``[start_scan_id, end_scan_id]``.  The pattern is
  interned per-rung so two PatternRef ranges can share the same
  underlying :class:`PMap`.
- :class:`AlternatingRun` — period-2 alternation between two patterns.
  Lookup picks ``pattern_on_even`` when
  ``(scan_id - start_scan_id) % 2 == 0`` and ``pattern_on_odd``
  otherwise — parity relative to the run's start, NOT to ``scan_id``
  itself, so fork points with odd anchors don't invert the answer.
- :class:`FiredOnly` — the rung fired but we no longer track per-scan
  values.  Triggered one-way when the intern dict reaches
  ``_FIRED_ONLY_THRESHOLD`` (100) distinct patterns.  Lookups return
  a sentinel PMap keyed on every tag the rung was ever observed to
  write; ``cause()``'s value-match test fails on the sentinel (fired-
  only rungs drop out of recorded causal chains past the transition,
  matching the design-doc trade-off), while ``effect()``'s PDG
  fallback filters through history.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from pyrsistent import PMap, pmap

# Threshold for the one-way cycle -> fired-only transition.  Bounded
# per-rung cost: 100 * ~500 bytes = ~50 KB worst case.
_FIRED_ONLY_THRESHOLD = 100

# Sentinel value attached to every tag in a FiredOnly rung's synthesized
# write PMap.  ``cause()``'s ``writes[tag] == transition.to_value`` check
# fails on this sentinel (object identity + no bool/number equality),
# so fired-only rungs do not re-enter the recorded backward walk after
# their promotion.  Analysis that needs the actual values for a given
# scan must replay to that scan via ``PLC.replay_to`` and read state.
_FIRED_ONLY_SENTINEL: Any = object()


@dataclass(frozen=True)
class PatternRef:
    """Timeline payload: the rung fired ``pattern`` for the whole range.

    ``pattern`` is a reference to the canonical PMap in the per-rung
    intern pool — two ranges with identical patterns share the same
    PMap instance.
    """

    pattern: PMap


@dataclass(frozen=True)
class AlternatingRun:
    """Timeline payload: period-2 alternation anchored at ``start_scan_id``.

    Parity is relative to the containing range's ``start_scan_id``, not
    to ``scan_id`` directly.  A naive ``scan_id % 2`` lookup inverts
    half the time when the run begins on an odd-anchored scan — guard
    against that trap explicitly in every consumer.
    """

    pattern_on_even: PMap
    pattern_on_odd: PMap


@dataclass(frozen=True)
class FiredOnly:
    """Timeline payload: the rung fired but per-scan values are discarded.

    Lookups synthesize a PMap keyed on every tag the rung was ever
    observed to write, each mapped to :data:`_FIRED_ONLY_SENTINEL`.
    """


FiringPayload = PatternRef | AlternatingRun | FiredOnly


@dataclass(frozen=True)
class RungFiringRange:
    """A span of scans on one rung's timeline, with an attached payload."""

    start_scan_id: int  # inclusive
    end_scan_id: int  # inclusive
    payload: FiringPayload


RungMode = Literal["cycle", "fired_only"]


class RungFiringTimelines:
    """Per-rung timelines of firing ranges with interning and mode tracking.

    The storage is append-only at the tail under normal operation —
    ``_commit_scan`` passes scan_ids in monotonically increasing order.
    Random-access lookup uses binary search over each rung's range list.

    Three internal state maps are kept in lockstep, one entry per rung
    that has ever fired:

    - ``_timelines``: ordered range list (sorted by ``start_scan_id``).
    - ``_intern``: canonical PMap pool for ``PatternRef`` / ``AlternatingRun``
      payloads.  Dropped for a rung once it transitions to fired-only.
    - ``_mode``: ``"cycle"`` until the intern pool hits the threshold,
      then ``"fired_only"`` permanently.  One-way — a rung that has
      been promoted never goes back, which keeps the A,B,A detection
      machinery from ever needing to re-examine the earlier ranges.

    Intern pools are per-rung (not global).  Two rungs that happen to
    produce identical PMaps keep their own canonical copies — this
    simplifies eviction (only walk the rung's own pool) and keeps the
    fired-only transition a rung-local decision.
    """

    __slots__ = (
        "_timelines",
        "_intern",
        "_mode",
        "_fired_only_writes",
    )

    def __init__(self) -> None:
        self._timelines: dict[int, list[RungFiringRange]] = {}
        self._intern: dict[int, dict[PMap, PMap]] = {}
        self._mode: dict[int, RungMode] = {}
        # Per-rung synthesized sentinel PMap returned by ``at()`` for
        # rungs in fired-only mode.  Built once at promotion from the
        # union of all tag names the intern pool had observed.
        self._fired_only_writes: dict[int, PMap] = {}

    # ---------------------------------------------------------------
    # Append path
    # ---------------------------------------------------------------

    def append(self, rung_index: int, scan_id: int, writes: PMap) -> None:
        """Record that ``rung_index`` fired on ``scan_id`` with ``writes``.

        Must be called with ``scan_id`` strictly greater than the
        rung's last recorded firing (append-only in practice).
        """
        mode = self._mode.get(rung_index, "cycle")
        timeline = self._timelines.get(rung_index)

        if mode == "fired_only":
            self._append_fired_only(rung_index, scan_id, timeline)
            return

        # Cycle mode: intern the pattern, then decide whether to extend
        # the previous range, collapse into an AlternatingRun, or start
        # a new range.  After every append, check whether the intern
        # pool crossed the threshold and promote if so.
        intern = self._intern.setdefault(rung_index, {})
        canonical = intern.get(writes)
        if canonical is None:
            intern[writes] = writes
            canonical = writes

        if timeline is None:
            self._timelines[rung_index] = [RungFiringRange(scan_id, scan_id, PatternRef(canonical))]
        else:
            self._append_cycle(timeline, scan_id, canonical)

        if len(intern) >= _FIRED_ONLY_THRESHOLD:
            self._promote_to_fired_only(rung_index)

    def _append_cycle(
        self,
        timeline: list[RungFiringRange],
        scan_id: int,
        canonical: PMap,
    ) -> None:
        """Extend / collapse / append a new range in cycle mode."""
        last = timeline[-1]

        # Extending: same canonical pattern as the current range's tail.
        if isinstance(last.payload, PatternRef) and last.payload.pattern is canonical:
            # Adjacent scan -> extend.  Non-adjacent (gap in firings)
            # breaks the run and starts a new range.
            if last.end_scan_id == scan_id - 1:
                timeline[-1] = RungFiringRange(last.start_scan_id, scan_id, last.payload)
                return
        elif isinstance(last.payload, AlternatingRun):
            # Extend the alternation only if adjacent AND the incoming
            # pattern matches the expected parity slot.
            if last.end_scan_id == scan_id - 1:
                expected = self._alternating_expected(last, scan_id)
                if expected is canonical:
                    timeline[-1] = RungFiringRange(last.start_scan_id, scan_id, last.payload)
                    return

        # A,B,A collapse: need two prior length-1 PatternRef ranges
        # with patterns (Y, X) followed by incoming X, all three scans
        # contiguous.  The collapse replaces all three with a single
        # AlternatingRun anchored at the first (Y) scan, with the
        # even/odd slots resolved from the Y anchor.
        if len(timeline) >= 2:
            prev = timeline[-1]
            prev_prev = timeline[-2]
            if (
                prev.start_scan_id == prev.end_scan_id == scan_id - 1
                and prev_prev.start_scan_id == prev_prev.end_scan_id == scan_id - 2
                and isinstance(prev.payload, PatternRef)
                and isinstance(prev_prev.payload, PatternRef)
                and prev.payload.pattern is not prev_prev.payload.pattern
                and prev_prev.payload.pattern is canonical
            ):
                # Anchor at prev_prev (scan_id - 2).  Even slot holds the
                # anchor's pattern, odd slot holds the middle pattern.
                alt = AlternatingRun(
                    pattern_on_even=prev_prev.payload.pattern,
                    pattern_on_odd=prev.payload.pattern,
                )
                timeline[-2:] = [RungFiringRange(prev_prev.start_scan_id, scan_id, alt)]
                return

        # Default: start a new length-1 PatternRef range.
        timeline.append(RungFiringRange(scan_id, scan_id, PatternRef(canonical)))

    @staticmethod
    def _alternating_expected(last: RungFiringRange, scan_id: int) -> PMap:
        """Which pattern an ``AlternatingRun`` predicts for ``scan_id``.

        ``scan_id`` is expected to be ``last.end_scan_id + 1``; the
        caller verifies adjacency before using the return value.
        """
        assert isinstance(last.payload, AlternatingRun)
        parity = (scan_id - last.start_scan_id) % 2
        return last.payload.pattern_on_even if parity == 0 else last.payload.pattern_on_odd

    def _append_fired_only(
        self,
        rung_index: int,
        scan_id: int,
        timeline: list[RungFiringRange] | None,
    ) -> None:
        """Append or extend a ``FiredOnly`` range for a promoted rung."""
        if timeline is None:
            self._timelines[rung_index] = [RungFiringRange(scan_id, scan_id, FiredOnly())]
            return
        last = timeline[-1]
        if isinstance(last.payload, FiredOnly) and last.end_scan_id == scan_id - 1:
            timeline[-1] = RungFiringRange(last.start_scan_id, scan_id, last.payload)
        else:
            timeline.append(RungFiringRange(scan_id, scan_id, FiredOnly()))

    def _promote_to_fired_only(self, rung_index: int) -> None:
        """One-way transition: drop the intern pool, snapshot observed tags.

        Existing ``PatternRef`` / ``AlternatingRun`` ranges stay in the
        timeline — ``at()`` handles all three payload types.  Future
        appends become ``FiredOnly`` ranges, synthesized against the
        rung's observed-tag union.
        """
        intern = self._intern.pop(rung_index, {})
        tag_names: set[str] = set()
        for pattern in intern:
            tag_names.update(pattern.keys())
        self._fired_only_writes[rung_index] = pmap(
            {name: _FIRED_ONLY_SENTINEL for name in tag_names}
        )
        self._mode[rung_index] = "fired_only"

    # ---------------------------------------------------------------
    # Lookup path
    # ---------------------------------------------------------------

    def at(self, scan_id: int) -> PMap:
        """Return the outer ``PMap[rung_index, PMap[tag, value]]`` at ``scan_id``.

        Iterates each rung's timeline with a binary search — O(R log S)
        where S is the per-rung range count.  Rungs whose timeline
        doesn't cover ``scan_id`` contribute nothing.
        """
        out: dict[int, PMap] = {}
        for rung_index, timeline in self._timelines.items():
            range_ = _binary_search_range(timeline, scan_id)
            if range_ is None:
                continue
            payload = range_.payload
            if isinstance(payload, PatternRef):
                out[rung_index] = payload.pattern
            elif isinstance(payload, AlternatingRun):
                parity = (scan_id - range_.start_scan_id) % 2
                out[rung_index] = payload.pattern_on_even if parity == 0 else payload.pattern_on_odd
            else:
                out[rung_index] = self._fired_only_writes[rung_index]
        return pmap(out)

    def fired_on(self, scan_id: int) -> set[int]:
        """Rung indices whose timelines cover ``scan_id``.

        Cheaper than :meth:`at` when only the identity set is needed
        (``query.cold_rungs`` / ``query.hot_rungs`` et al.).
        """
        fired: set[int] = set()
        for rung_index, timeline in self._timelines.items():
            if _binary_search_range(timeline, scan_id) is not None:
                fired.add(rung_index)
        return fired

    def ever_fired(self) -> set[int]:
        """Rung indices with at least one range in their timeline."""
        return {idx for idx, tl in self._timelines.items() if tl}

    # ---------------------------------------------------------------
    # Lifecycle
    # ---------------------------------------------------------------

    def reset(self) -> None:
        """Clear all state — used on reboot / reset."""
        self._timelines.clear()
        self._intern.clear()
        self._mode.clear()
        self._fired_only_writes.clear()

    def mode(self, rung_index: int) -> RungMode:
        """Current mode of the rung's timeline.  Default: ``"cycle"``."""
        return self._mode.get(rung_index, "cycle")

    def intern_size(self, rung_index: int) -> int:
        """Distinct patterns currently in the rung's intern pool.

        Zero for rungs that have transitioned to ``fired_only`` (the
        pool is dropped at promotion).
        """
        return len(self._intern.get(rung_index, {}))


def _binary_search_range(timeline: list[RungFiringRange], scan_id: int) -> RungFiringRange | None:
    """Return the range covering ``scan_id``, or ``None`` if none covers it.

    Assumes ``timeline`` is sorted by ``start_scan_id`` and ranges are
    disjoint (invariant maintained by the append path).
    """
    lo, hi = 0, len(timeline)
    while lo < hi:
        mid = (lo + hi) // 2
        r = timeline[mid]
        if scan_id < r.start_scan_id:
            hi = mid
        elif scan_id > r.end_scan_id:
            lo = mid + 1
        else:
            return r
    return None
