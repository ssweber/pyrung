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
class ArithmeticRun:
    """Timeline payload: tags with a constant per-scan integer delta.

    ``base_pattern`` holds the PMap at ``start_scan_id``.  ``deltas``
    maps tag names to their per-scan integer increment; tags absent
    from ``deltas`` are constant across the range.

    Value at *scan_id*::

        base_pattern[tag] + deltas[tag] * (scan_id - start_scan_id)

    Detection mirrors the A,B,A collapse — three contiguous single-scan
    ``PatternRef`` ranges whose per-tag differences are constant.
    Extension bypasses the intern pool entirely, so a timer accumulator
    ticking +1 per scan never grows the pool past its initial 3 entries.
    """

    base_pattern: PMap
    deltas: PMap  # tag_name -> int


@dataclass(frozen=True)
class FiredOnly:
    """Timeline payload: the rung fired but per-scan values are discarded.

    Lookups synthesize a PMap keyed on every tag the rung was ever
    observed to write, each mapped to :data:`_FIRED_ONLY_SENTINEL`.
    """


FiringPayload = PatternRef | AlternatingRun | ArithmeticRun | FiredOnly


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

        # Fast path: extend an active ArithmeticRun without interning.
        # This is the key win — a timer accumulator ticking +1/scan
        # never adds entries to the intern pool after the initial collapse.
        if timeline:
            last = timeline[-1]
            payload = last.payload
            if isinstance(payload, ArithmeticRun) and last.end_scan_id == scan_id - 1:
                if _arithmetic_matches(payload, last.start_scan_id, scan_id, writes):
                    timeline[-1] = RungFiringRange(last.start_scan_id, scan_id, payload)
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

        # Collapse detection: two prior length-1 PatternRef ranges plus
        # the incoming scan, all three contiguous.  Two flavors:
        #   A,B,A  → AlternatingRun   (identity: prev_prev pattern IS canonical)
        #   V,V+d,V+2d → ArithmeticRun (constant integer deltas)
        if len(timeline) >= 2:
            prev = timeline[-1]
            prev_prev = timeline[-2]
            if (
                prev.start_scan_id == prev.end_scan_id == scan_id - 1
                and prev_prev.start_scan_id == prev_prev.end_scan_id == scan_id - 2
                and isinstance(prev.payload, PatternRef)
                and isinstance(prev_prev.payload, PatternRef)
            ):
                pp_pat = prev_prev.payload.pattern
                p_pat = prev.payload.pattern

                # A,B,A → AlternatingRun
                if pp_pat is not p_pat and pp_pat is canonical:
                    alt = AlternatingRun(
                        pattern_on_even=pp_pat,
                        pattern_on_odd=p_pat,
                    )
                    timeline[-2:] = [RungFiringRange(prev_prev.start_scan_id, scan_id, alt)]
                    return

                # V, V+d, V+2d → ArithmeticRun
                deltas = _compute_arithmetic_deltas(pp_pat, p_pat, canonical)
                if deltas is not None:
                    arith = ArithmeticRun(
                        base_pattern=pp_pat,
                        deltas=pmap(deltas),
                    )
                    timeline[-2:] = [RungFiringRange(prev_prev.start_scan_id, scan_id, arith)]
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
            elif isinstance(payload, ArithmeticRun):
                out[rung_index] = _reconstruct_arithmetic(payload, range_.start_scan_id, scan_id)
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

    def rung_writes_at(self, rung_index: int, scan_id: int) -> PMap | None:
        """Return the writes for a single rung at ``scan_id``, or ``None``.

        O(log S) per call — binary search over the rung's range list.
        """
        timeline = self._timelines.get(rung_index)
        if timeline is None:
            return None
        range_ = _binary_search_range(timeline, scan_id)
        if range_ is None:
            return None
        payload = range_.payload
        if isinstance(payload, PatternRef):
            return payload.pattern
        if isinstance(payload, AlternatingRun):
            parity = (scan_id - range_.start_scan_id) % 2
            return payload.pattern_on_even if parity == 0 else payload.pattern_on_odd
        if isinstance(payload, ArithmeticRun):
            return _reconstruct_arithmetic(payload, range_.start_scan_id, scan_id)
        return self._fired_only_writes[rung_index]

    def last_tag_write_before(
        self,
        writer_indices: frozenset[int],
        tag_name: str,
        before_scan_id: int,
    ) -> tuple[int, Any] | None:
        """Find the most recent scan < ``before_scan_id`` where any rung in
        ``writer_indices`` wrote ``tag_name``.

        Returns ``(scan_id, value)`` or ``None``.  For ``FiredOnly``
        payloads the value is the sentinel — callers that need a real
        value must fall back to state reads.  Iterates range lists
        backward; O(W × log S) where W = ``len(writer_indices)``.
        """
        best_scan: int | None = None
        best_value: Any = None
        for rung_index in writer_indices:
            timeline = self._timelines.get(rung_index)
            if not timeline:
                continue
            result = _last_tag_write_in_timeline(
                timeline,
                rung_index,
                tag_name,
                before_scan_id,
                self._fired_only_writes.get(rung_index),
            )
            if result is not None:
                scan, value = result
                if best_scan is None or scan > best_scan:
                    best_scan = scan
                    best_value = value
        if best_scan is None:
            return None
        return (best_scan, best_value)

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

    def trim_before(self, min_scan_id: int) -> None:
        """Drop firing data older than ``min_scan_id``.

        Designed to run together with scan-log trimming so the two
        datasets stay in lockstep.  For each rung:

        - Ranges entirely before ``min_scan_id`` are removed.
        - A range straddling ``min_scan_id`` has its ``start_scan_id``
          advanced to ``min_scan_id``.  For ``AlternatingRun``
          payloads, advancing by an odd delta swaps ``pattern_on_even``
          and ``pattern_on_odd`` — the parity is anchored at the new
          ``start_scan_id``, and odd advances reverse which canonical
          pattern lands on the even/odd slots.
        - The rung's intern pool is walked afterward; patterns no
          longer referenced by any surviving range are dropped.
          Fired-only rungs have no intern pool to walk.

        No caller exists yet — log trimming lands in a later stage.
        The hook ships now so it grows in lockstep with the rest of
        the record-and-replay machinery rather than as a retrofit.
        """
        if min_scan_id <= 0:
            return
        for rung_index in list(self._timelines):
            timeline = self._timelines[rung_index]
            kept: list[RungFiringRange] = []
            for range_ in timeline:
                if range_.end_scan_id < min_scan_id:
                    continue
                if range_.start_scan_id < min_scan_id:
                    delta = min_scan_id - range_.start_scan_id
                    kept.append(_advance_range_start(range_, delta))
                else:
                    kept.append(range_)
            if kept:
                self._timelines[rung_index] = kept
            else:
                # Rung fully trimmed: drop every associated cache slot
                # so the rung reverts to its initial never-fired state
                # (intern pool empty, mode back to cycle, no fired-only
                # sentinel).  Subsequent appends begin a fresh timeline.
                del self._timelines[rung_index]
                self._intern.pop(rung_index, None)
                self._mode.pop(rung_index, None)
                self._fired_only_writes.pop(rung_index, None)
                continue
            # Prune the intern pool to patterns still referenced by
            # some surviving range.  Fired-only rungs already have
            # an empty pool; skip them.
            if self._mode.get(rung_index, "cycle") == "fired_only":
                continue
            intern = self._intern.get(rung_index)
            if not intern:
                continue
            live: set[int] = set()
            for range_ in kept:
                payload = range_.payload
                if isinstance(payload, PatternRef):
                    live.add(id(payload.pattern))
                elif isinstance(payload, AlternatingRun):
                    live.add(id(payload.pattern_on_even))
                    live.add(id(payload.pattern_on_odd))
                elif isinstance(payload, ArithmeticRun):
                    live.add(id(payload.base_pattern))
            self._intern[rung_index] = {
                pattern: canonical for pattern, canonical in intern.items() if id(canonical) in live
            }


def _advance_range_start(range_: RungFiringRange, delta: int) -> RungFiringRange:
    """Advance a range's ``start_scan_id`` by ``delta``, preserving semantics.

    For ``AlternatingRun``, an odd delta swaps ``pattern_on_even`` and
    ``pattern_on_odd``.  For ``ArithmeticRun``, the base pattern is
    rebased to the new start by applying ``deltas * delta``.
    """
    assert delta > 0, "advance must move the start forward"
    payload = range_.payload
    new_start = range_.start_scan_id + delta
    if isinstance(payload, AlternatingRun) and delta % 2 == 1:
        payload = AlternatingRun(
            pattern_on_even=payload.pattern_on_odd,
            pattern_on_odd=payload.pattern_on_even,
        )
    elif isinstance(payload, ArithmeticRun):
        new_base = payload.base_pattern
        for tag, per_scan_d in payload.deltas.items():
            new_base = new_base.set(tag, payload.base_pattern[tag] + per_scan_d * delta)
        payload = ArithmeticRun(base_pattern=new_base, deltas=payload.deltas)
    return RungFiringRange(new_start, range_.end_scan_id, payload)


def _compute_arithmetic_deltas(p0: PMap, p1: PMap, p2: PMap) -> dict[str, int] | None:
    """Check if three patterns form an arithmetic progression.

    Returns ``{tag_name: delta}`` for tags that change by a constant
    integer amount, or ``None`` if the patterns don't qualify.  All
    three must have the same key set, and at least one tag must have
    a non-zero integer delta.
    """
    if set(p0.keys()) != set(p1.keys()) or set(p1.keys()) != set(p2.keys()):
        return None
    deltas: dict[str, int] = {}
    for tag in p0:
        v0, v1, v2 = p0[tag], p1[tag], p2[tag]
        if v0 == v1 == v2:
            continue
        if not (
            isinstance(v0, int)
            and not isinstance(v0, bool)
            and isinstance(v1, int)
            and not isinstance(v1, bool)
            and isinstance(v2, int)
            and not isinstance(v2, bool)
        ):
            return None
        d = v1 - v0
        if v2 - v1 != d:
            return None
        deltas[tag] = d
    return deltas if deltas else None


def _arithmetic_matches(
    payload: ArithmeticRun, start_scan_id: int, scan_id: int, writes: PMap
) -> bool:
    """True if ``writes`` matches the ArithmeticRun's prediction at ``scan_id``."""
    if set(writes.keys()) != set(payload.base_pattern.keys()):
        return False
    offset = scan_id - start_scan_id
    for tag in writes:
        if tag in payload.deltas:
            if writes[tag] != payload.base_pattern[tag] + payload.deltas[tag] * offset:
                return False
        elif writes[tag] != payload.base_pattern[tag]:
            return False
    return True


def _reconstruct_arithmetic(payload: ArithmeticRun, start_scan_id: int, scan_id: int) -> PMap:
    """Reconstruct the full write PMap at ``scan_id`` from an ArithmeticRun."""
    offset = scan_id - start_scan_id
    result = payload.base_pattern
    for tag, delta in payload.deltas.items():
        result = result.set(tag, payload.base_pattern[tag] + delta * offset)
    return result


def _last_tag_write_in_timeline(
    timeline: list[RungFiringRange],
    rung_index: int,
    tag_name: str,
    before_scan_id: int,
    fired_only_writes: PMap | None,
) -> tuple[int, Any] | None:
    """Find the most recent scan < ``before_scan_id`` where this rung wrote ``tag_name``.

    Walks ranges backward from the tail.  Returns ``(scan_id, value)``
    or ``None``.
    """
    for i in range(len(timeline) - 1, -1, -1):
        range_ = timeline[i]
        if range_.start_scan_id >= before_scan_id:
            continue
        # Clamp to just before before_scan_id
        effective_end = min(range_.end_scan_id, before_scan_id - 1)
        payload = range_.payload
        if isinstance(payload, PatternRef):
            val = payload.pattern.get(tag_name)
            if val is not None:
                return (effective_end, val)
        elif isinstance(payload, AlternatingRun):
            # Check effective_end and effective_end-1 (covers both parities)
            for scan in (effective_end, effective_end - 1):
                if scan < range_.start_scan_id:
                    continue
                parity = (scan - range_.start_scan_id) % 2
                pat = payload.pattern_on_even if parity == 0 else payload.pattern_on_odd
                val = pat.get(tag_name)
                if val is not None:
                    return (scan, val)
        elif isinstance(payload, ArithmeticRun):
            if tag_name in payload.deltas:
                offset = effective_end - range_.start_scan_id
                return (
                    effective_end,
                    payload.base_pattern[tag_name] + payload.deltas[tag_name] * offset,
                )
            else:
                val = payload.base_pattern.get(tag_name)
                if val is not None:
                    return (effective_end, val)
        else:
            # FiredOnly — return sentinel value
            if fired_only_writes is not None and tag_name in fired_only_writes:
                return (effective_end, fired_only_writes[tag_name])
    return None


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
