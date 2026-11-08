"""Columnar, range-compressed rung firing evidence.

Three independent facts are retained:

* ``rung -> fired scan ranges``
* ``(rung, tag) -> final attempted value ranges``
* ``(rung, tag) -> scans with more than one attempted value``

The last fact is sparse: false is implicit.  Exact instruction order, reads,
and individual write occurrences deliberately do not live here; consumers
replay only the selected kernel scans when they need that evidence.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Callable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import islice
from typing import Any, ClassVar, Generic, TypeVar

from pyrsistent import PMap, pmap

K = TypeVar("K")

# Scans a question may step again to get the records of a compiled run, about what the
# run would have cost stepped.  Past this it is an error rather than a wait nobody asked for.
SETTLE_SCAN_LIMIT = 250_000


class UnrecordedScansError(ValueError):
    """A question reaches more compiled scans than it may step again for their records."""

    def __init__(self, first: int, last: int, scans: int) -> None:
        super().__init__(
            f"scans {first}..{last} include some that ran compiled and have no records yet. "
            f"Getting their records means stepping {scans} scans again from the nearest "
            f"checkpoint, and a question may step at most {SETTLE_SCAN_LIMIT}. "
            "Scans a question has stepped keep their records, so ask first about a scan nearer "
            "the start of that range for cause, or nearer its end for effect, then ask again. "
            "From Python, cause() also takes since=, and jit=False records a run as it goes."
        )
        self.first = first
        self.last = last
        self.scans = scans


class SearchQuestionRefused(BaseException):
    """A how() asked a question that met :class:`UnrecordedScansError`.

    The search reads causes under many ``except Exception`` that mean "no evidence", and scans
    it cannot afford to read are not evidence that nothing happened in them.  So in a search
    the refusal is a BaseException, whichever catch it passes, and ends the search with a
    terminal saying so.
    """

    def __init__(self, refusal: UnrecordedScansError) -> None:
        super().__init__(str(refusal))
        self.refusal = refusal


# The ranges a column holds before it keeps later values as Unknown, to bound its memory.
_ENTRY_LIMIT = 1_000
_UNKNOWN_VALUE = object()
_MISSING_WRITE = object()


@dataclass(frozen=True, slots=True)
class ScanRange:
    start_scan_id: int
    end_scan_id: int


@dataclass(frozen=True, slots=True)
class Constant:
    value: Any


@dataclass(frozen=True, slots=True)
class Alternating:
    value_on_even: Any
    value_on_odd: Any


@dataclass(frozen=True, slots=True)
class Arithmetic:
    base: int
    delta: int


@dataclass(frozen=True, slots=True)
class Unknown:
    pass


ValuePayload = Constant | Alternating | Arithmetic | Unknown


@dataclass(frozen=True, slots=True)
class ValueRange:
    start_scan_id: int
    end_scan_id: int
    payload: ValuePayload


@dataclass(frozen=True, slots=True)
class Repeat:
    """Ranges that repeat every *period* scans, *count* times, in one entry.

    ``ranges`` are the first repetition's.  Repetition r is the same ranges moved r * period
    scans later.  Each range's values also move by r times its entry in ``steps``, which is
    empty when nothing steps.  A step applies only to whole-number values.  A list holding a
    repeat reads as the list with every repetition written out.  That is the list that
    appending each scan would have made.
    """

    start_scan_id: int
    end_scan_id: int
    period: int
    ranges: tuple[Any, ...]
    count: int
    steps: tuple[int, ...] = ()


class RungFiringTimelines(Generic[K]):
    """Append-only columnar firing index, generic over rung identity.

    A column stores at most 1,000 entries (``_ENTRY_LIMIT``), to bound its memory.  On the scan
    that would store one more, the column turns Unknown.  A repeat counts as every range it
    stands for.  A gap that has not settled can turn it Unknown early.
    """

    __slots__ = (
        "_fired_ranges",
        "_value_timelines",
        "_varied_ranges",
        "_tags_by_rung",
        "_writers_by_tag",
        "_entries",
        "_dropped",
        "_floors",
        "_unknown_keys",
        "_partly_unknown",
    )

    # A test sets this to check a record after each change.  Each method that changes one
    # calls it last.
    _check: ClassVar[Callable[[RungFiringTimelines[Any]], None] | None] = None

    def __init__(self) -> None:
        self._fired_ranges: dict[K, list[ScanRange]] = {}
        self._value_timelines: dict[tuple[K, str], list[ValueRange]] = {}
        self._varied_ranges: dict[tuple[K, str], list[ScanRange]] = {}
        self._tags_by_rung: dict[K, set[str]] = {}
        self._writers_by_tag: dict[str, set[K]] = {}
        # The ranges each column holds, with a repeat written out, until it turns Unknown.
        self._entries: dict[tuple[K, str], int] = {}
        # The entries trims from the front took out of each column.  Stepping counts them still.
        self._dropped: dict[tuple[K, str], int] = {}
        # The first scan each trimmed column holds, and the last two ranges the trim took off.
        # A settle of a gap at that scan appends to those two ranges, as stepping did.
        self._floors: dict[tuple[K, str], tuple[int, list[ValueRange]]] = {}
        # Each column that turned Unknown, and the scan whose entry reached the limit.
        self._unknown_keys: dict[tuple[K, str], int] = {}
        # Columns with a range a stretch could not speak for.  Later scans still say.
        self._partly_unknown: set[tuple[K, str]] = set()

    def append(
        self,
        rung_index: K,
        scan_id: int,
        writes: Mapping[str, Any],
        varied: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        """Record one firing and its final attempted values."""
        _append_scan_range(self._fired_ranges.setdefault(rung_index, []), scan_id)
        tags = self._tags_by_rung.setdefault(rung_index, set())
        for tag_name in tags:
            if tag_name not in writes:
                key = (rung_index, tag_name)
                self._push(key, self._value_timelines[key], scan_id, _MISSING_WRITE)
        for tag_name, value in writes.items():
            key = (rung_index, tag_name)
            if tag_name not in tags:
                tags.add(tag_name)
            self._writers_by_tag.setdefault(tag_name, set()).add(rung_index)
            if value is _UNKNOWN_VALUE:
                self._partly_unknown.add(key)
            self._push(key, self._value_timelines.setdefault(key, []), scan_id, value)
            if tag_name in varied:
                _append_scan_range(self._varied_ranges.setdefault(key, []), scan_id)
        if RungFiringTimelines._check is not None:
            RungFiringTimelines._check(self)

    def _done(self) -> None:
        if RungFiringTimelines._check is not None:
            RungFiringTimelines._check(self)

    def _push(self, key: tuple[K, str], timeline: list[Any], scan_id: int, value: Any) -> None:
        """Append one scan's value to a column, which turns Unknown at its last entry."""
        unknown = self._unknown_keys
        if key in unknown:
            _append_value(timeline, scan_id, value if value is _MISSING_WRITE else _UNKNOWN_VALUE)
            return
        grown = _append_value(timeline, scan_id, value)
        if grown:
            entries = self._entries.get(key, 0) + grown
            if entries >= _ENTRY_LIMIT:
                unknown[key] = scan_id
                self._entries.pop(key, None)
            else:
                self._entries[key] = entries

    def append_repeated(
        self,
        rung_index: K,
        first_scan_id: int,
        count: int,
        writes: Mapping[str, Any],
        varied: frozenset[str] | set[str] = frozenset(),
        deltas: Mapping[str, int] | None = None,
        unknown: frozenset[str] | set[str] = frozenset(),
    ) -> None:
        """As if :meth:`append` ran for each of *count* scans from *first_scan_id*.

        *writes* are the values on the first scan.  A tag in *deltas* moves by that much
        each scan, and a tag in *unknown* reads as ``Unknown`` over the whole range.
        """
        last = first_scan_id + count - 1
        for tag in self._tags_by_rung.get(rung_index, ()):
            if tag not in writes:
                self.append_tag_run(rung_index, tag, first_scan_id, last, _MISSING_WRITE)
        self.append_fired_run(rung_index, first_scan_id, last)
        for tag, value in writes.items():
            if tag in unknown:
                value, delta = _UNKNOWN_VALUE, None
            else:
                delta = (deltas or {}).get(tag)
            self.append_tag_run(rung_index, tag, first_scan_id, last, value, delta, tag in varied)
        self._done()

    def run_count(self) -> int:
        """The ranges held, which is what these timelines cost."""
        return sum(map(len, self._fired_ranges.values())) + sum(
            map(len, self._value_timelines.values())
        )

    def plan_repeat(self, last_scan_id: int, period: int) -> RepeatPlan | None:
        """How to record more cycles of the last *period* scans, or None if it cannot.

        The caller promises that each later cycle appends what the last cycle appended, moved
        one period on.  Each whole number also moves as it moved between the last two cycles.
        Each column must have taken the same shape on its last cycles.  A column's cycle may
        start on any scan of the period.  Then appending the later cycles scan by scan would
        repeat that shape.  So each later cycle reads back as appending it would have.
        """
        actions: list[tuple[Any, list[Any], Any]] = []
        tables: tuple[tuple[Mapping[Any, list[Any]], bool], ...] = (
            (self._fired_ranges, False),
            (self._value_timelines, True),
            (self._varied_ranges, False),
        )
        for table, values in tables:
            for key, ranges in table.items():
                action = self._repeat_action(key, ranges, last_scan_id, period, values)
                if action is False:
                    return None
                if action is not None:
                    actions.append((key if values else None, ranges, action))
        return RepeatPlan(self, actions, period)

    def _repeat_action(
        self, key: Any, ranges: list[Any], last: int, period: int, values: bool
    ) -> Any:
        """None for a column the cycles leave alone, False if it cannot repeat."""
        if not ranges:
            return None
        end = ranges[-1].end_scan_id
        if end > last:
            return False
        if end <= last - 3 * period:
            return None
        if end <= last - period:
            return False  # written lately, but not in the last cycle
        tail = _last_written_out(ranges)
        if tail.start_scan_id <= last - 2 * period + 1 and end == last:
            # One range runs through the last two cycles, so each later scan extends it.  A
            # count keeps its rate, and any other range must hold the same values a cycle on.
            # Any payload but a count gives a value set by the scan's parity alone, so the
            # first two scans of the cycle stand for all of them.
            first = last - period + 1
            if values and key in self._unknown_keys and not _unknown_or_missing(tail):
                return False  # Stepping writes Unknown into it from now on.
            if (
                values
                and not isinstance(tail.payload, Arithmetic)
                and not all(
                    _same_value(_value_at(tail, scan_id), _value_at(tail, scan_id + period))
                    for scan_id in range(first, min(first + 2, last + 1))
                )
            ):
                return False
            return _EXTEND
        # A column's own cycle may start on any scan of the period, so the cycle runs from
        # the end of one of its ranges.  A count that goes up mid-cycle needs that.
        for range_ in reversed(list(_expanded(ranges, last - period + 1, last))):
            action = self._cycle_action(key, ranges, range_.end_scan_id, last, period, values)
            if action is not None:
                return action
        return False

    def _cycle_action(
        self, key: Any, ranges: list[Any], edge: int, last: int, period: int, values: bool
    ) -> _Cycle | None:
        """The column's last cycles ending at scan *edge*, if they repeat with steps."""
        # Appending a scan looks back two scans at most.  So two cycles of two scans or more
        # show what a later cycle does, and a cycle of one scan needs three.
        cycles = []
        for back in (2, 1, 0) if period == 1 else (1, 0):
            first = edge - (back + 1) * period + 1
            cycle = list(_expanded(ranges, first, first + period - 1))
            if any(
                range_.start_scan_id < first or range_.end_scan_id > first + period - 1
                for range_ in cycle
            ):
                return None
            cycles.append(cycle)
        if not cycles[-1]:
            return None
        steps = _cycle_steps(cycles[-2], cycles[-1], period)
        if steps is None or (
            len(cycles) == 3 and steps != _cycle_steps(cycles[0], cycles[1], period)
        ):
            return None
        cycle = tuple(cycles[-1])
        step = 0
        if not any(steps):
            steps = ()
        elif not _steps_keep_their_order(cycle, steps):
            return None
        else:
            step = max(steps, key=abs)
        # The scans after the edge start the next cycle.  They move with the rest.
        trailing = tuple(_expanded(ranges, edge + 1, last))
        if values and key in self._unknown_keys:
            # Stepping writes Unknown into a column that went unknown.  A cycle that holds a
            # value comes from before that, so it cannot repeat.
            if not all(_unknown_or_missing(range_) for range_ in cycle):
                return None
            return _Cycle(cycle, steps, step, trailing)
        if not values or key is None:  # A join counts nothing.
            return _Cycle(cycle, steps, step, trailing)
        # Each cycle adds the cycle's ranges to the column.  A range that flips or counts holds
        # one entry more on its second scan than after its third.  So the plan stops two short
        # of the limit, and the cycle that reaches it goes in scan by scan.
        limit = (_ENTRY_LIMIT - 2 - self._entries[key]) // len(cycle)
        if limit < 1:
            return None
        return _Cycle(cycle, steps, step, trailing, limit)

    def tags_of(self, rung_index: K) -> Iterable[str]:
        """Every tag this rung has written."""
        return self._tags_by_rung.get(rung_index, ())

    def append_fired_run(self, rung_index: K, first_scan_id: int, last_scan_id: int) -> None:
        """The firing half of :meth:`append`, for each scan of the range."""
        self._tags_by_rung.setdefault(rung_index, set())
        ranges = self._fired_ranges.setdefault(rung_index, [])
        _unroll_tail(ranges)
        if ranges and ranges[-1].end_scan_id == first_scan_id - 1:
            _extend_scan_range(ranges, last_scan_id)
        else:
            ranges.append(ScanRange(first_scan_id, last_scan_id))
        self._done()

    def append_tag_run(
        self,
        rung_index: K,
        tag: str,
        first_scan_id: int,
        last_scan_id: int,
        value: Any,
        delta: int | None = None,
        varied: bool = False,
    ) -> None:
        """One tag's half of :meth:`append`, for each scan of the range.

        *value* is what it reads on the first scan, ``_MISSING_WRITE`` if the rung does not write
        it there, and it moves by *delta* each scan.  A rung's tags ask nothing of each other, so
        they may go in in any order, and its firings with :meth:`append_fired_run`.
        """
        key = (rung_index, tag)
        if value is _MISSING_WRITE:
            if tag not in self._tags_by_rung.get(rung_index, ()):
                return  # A tag has no column until the rung first writes it.
        else:
            self._tags_by_rung.setdefault(rung_index, set()).add(tag)
            self._writers_by_tag.setdefault(tag, set()).add(rung_index)
            if value is _UNKNOWN_VALUE:
                self._partly_unknown.add(key)
            if varied:
                ranges = self._varied_ranges.setdefault(key, [])
                _append_scan_range(ranges, first_scan_id)
                _extend_scan_range(ranges, last_scan_id)
        if value is _UNKNOWN_VALUE:
            payload: ValuePayload = Unknown()
        else:
            payload = Arithmetic(value, delta) if delta else Constant(value)
        timeline = self._value_timelines.setdefault(key, [])
        self._append_run(key, timeline, ValueRange(first_scan_id, last_scan_id, payload))
        self._done()

    def _append_run(self, key: tuple[K, str], timeline: list[Any], range_: ValueRange) -> None:
        """Append each scan of *range_* as :meth:`append` would, until the tail carries the rest."""
        high = range_.end_scan_id
        for scan_id in range(range_.start_scan_id, high + 1):
            self._push(key, timeline, scan_id, _value_at(range_, scan_id))
            ahead = _as_unknown(range_) if key in self._unknown_keys else range_
            if scan_id < high and _carries(timeline[-1], ahead):
                tail = timeline[-1]
                timeline[-1] = ValueRange(tail.start_scan_id, high, tail.payload)
                return

    def replace_range(self, settled: RungFiringTimelines[K], first: int, last: int) -> None:
        """Take scans *first*..*last* from *settled*, an index of them made later.

        Each column then turns Unknown on the scan stepping would have, or on the scan it did
        already if that came first.
        """
        _replace_ranges(self._fired_ranges, settled._fired_ranges, _trim_scan_ranges, first, last)
        _replace_ranges(self._varied_ranges, settled._varied_ranges, _trim_scan_ranges, first, last)
        for key in settled._value_timelines.keys() | self._value_timelines.keys():
            middle = _trim_value_ranges(settled._value_timelines.get(key, []), first, last)
            mine = self._value_timelines.get(key, [])
            prefix = _trim_value_ranges(mine, None, first - 1)
            later = _trim_value_ranges(mine, last + 1, None)
            touched = next(_expanded(mine, first, last), None) is not None
            fired = self._fired_ranges.get(key[0], [])
            # A trim may have taken every scan the column held before the window.
            begun = bool(prefix or self._dropped.get(key))
            if (
                not middle
                and not touched
                and not (begun and next(_expanded(fired, first, last), None))
            ):
                continue  # Nothing to add, and the column stays as it was.
            if not (prefix or middle or later):
                self._value_timelines.pop(key, None)
                for table in (self._unknown_keys, self._entries, self._dropped, self._floors):
                    table.pop(key, None)
                continue
            column_from = first - 1 if begun else (middle or later)[0].start_scan_id
            prefix, dropped, floor = self._whole(key, prefix)
            if floor is None:
                self._floors.pop(key, None)  # The window starts the column again.
            # Readers that rank candidate scans see where each range starts.  So the ranges
            # at each join go in again as stepping appends them.
            timeline = _as_of(prefix, mine, first - 1)
            missing = _missing_writes(
                fired,
                max(first, column_from + 1),
                middle[0].start_scan_id - 1 if middle else last,
            )
            self._rejoined(timeline, [*missing, *middle], len(missing))
            missing = _missing_writes(
                fired,
                max(last + 1, column_from + 1),
                later[0].start_scan_id - 1 if later else None,
            )
            self._rejoined(timeline, [*missing, *later], len(missing))
            self._recount(key, timeline, first, dropped, settled._unknown_keys.get(key))
            if floor is not None:
                timeline = self._cut(key, timeline, dropped, floor)
            self._value_timelines[key] = timeline
        # A replay marks each column it wrote Unknown into.  The window keeps that Unknown.
        self._partly_unknown |= settled._partly_unknown
        self._rebuild_indexes()
        self._done()

    def _recount(
        self,
        key: tuple[K, str],
        timeline: list[Any],
        first: int,
        trimmed: int,
        replayed: int | None,
    ) -> None:
        """Count a column filled in from *first*, and turn it Unknown where stepping would.

        The scans after the window kept values this record counted without the window's
        ranges.  So the column may reach the limit sooner than this record said, never later.
        The replay may have turned the column Unknown inside the window.  It stays Unknown.
        """
        gone = self._unknown_keys.get(key)
        if gone is None or gone >= first:
            for found in (_limit_scan(timeline, trimmed), replayed):
                if found is not None and (gone is None or found < gone):
                    gone = found
        if gone is None:
            self._entries[key] = trimmed + _written_out_length(timeline)
            return
        self._unknown_keys[key] = gone
        self._entries.pop(key, None)
        start = max(gone + 1, first)
        rest = [_as_unknown(element) for element in _trim_value_ranges(timeline, start, None)]
        timeline[:] = _as_of(_trim_value_ranges(timeline, None, start - 1), timeline, start - 1)
        # Two ranges that held different values can both read Unknown now, so each goes in.
        self._rejoined(timeline, rest, len(rest))

    def _rejoined(self, timeline: list[Any], ranges: list[Any], appended_from: int = 0) -> None:
        """Join the settled ranges to the ranges on either side of the gap.

        The column then has the same ranges it would have had if those scans had been stepped.
        Appending a scan reads the last two ranges at most.  The ranges from *appended_from* on
        came from appending, and the first of them may be cut short.  So two ranges on from
        that one, the rest go on unchanged once the last two agree.  Nothing is counted.
        """
        for index, item in enumerate(ranges):
            if (
                index >= appended_from + 3
                and len(timeline) >= 2
                and _same_range(timeline[-1], ranges[index - 1])
                and _same_range(timeline[-2], ranges[index - 2])
            ):
                timeline.extend(ranges[index:])
                return
            if type(item) is Repeat:
                self._rejoined_repeat(timeline, item)
            else:
                _append_range(timeline, item)

    def _rejoined_repeat(self, timeline: list[Any], repeat: Repeat) -> None:
        """Append *repeat* as stepping appends it, as one repeat once its repetitions settle.

        Once the column's last cycles take one shape, a plan records the rest as a jump does.
        """
        _unroll_tail(timeline)
        period = repeat.period
        for index in range(repeat.count):
            for range_ in _repetition(repeat, index):
                _append_range(timeline, range_)
            rest = repeat.count - 1 - index
            if not rest:
                return
            action = self._repeat_action(None, timeline, timeline[-1].end_scan_id, period, True)
            if action:
                RepeatPlan(self, [(None, timeline, action)], period).apply(rest)
                return
            if index + 1 >= _REJOIN_REPETITIONS:
                timeline[:] = _coalesced(
                    [*timeline, *_repeats(repeat, index + 1, repeat.count - 1)]
                )
                return

    def fired_throughout(self, first_scan_id: int, last_scan_id: int) -> set[K]:
        """The rungs that fired on every scan of the range."""
        fired: set[K] = set()
        for rung_index, ranges in self._fired_ranges.items():
            range_ = _find_scan_range(ranges, first_scan_id)
            if range_ is not None and range_.end_scan_id >= last_scan_id:
                fired.add(rung_index)
        return fired

    def at(self, scan_id: int) -> PMap:
        """Reconstruct ``rung -> {tag: final_attempt}`` for one scan."""
        out: dict[K, PMap] = {}
        for rung_index in self.fired_on(scan_id):
            writes = self.rung_writes_at(rung_index, scan_id)
            if writes is not None:
                out[rung_index] = writes
        return pmap(out)

    def fired_on(self, scan_id: int) -> set[K]:
        return {
            rung_index
            for rung_index, ranges in self._fired_ranges.items()
            if _find_scan_range(ranges, scan_id) is not None
        }

    def latest_firing_scan_at_or_before(
        self, rung_indices: frozenset[K], scan_id: int
    ) -> int | None:
        best: int | None = None
        for rung_index in rung_indices:
            candidate = _latest_scan(self._fired_ranges.get(rung_index, []), scan_id)
            if candidate is not None and (best is None or candidate > best):
                best = candidate
        return best

    def latest_value_transition_scan_at_or_before(
        self,
        rung_indices: frozenset[K],
        tag_name: str,
        value: Any,
        scan_id: int,
        *,
        missing_is_unknown: bool = True,
    ) -> int | None:
        best: int | None = None
        for rung_index in rung_indices:
            timeline = self._value_timelines.get((rung_index, tag_name), [])
            candidate = _latest_value_transition(timeline, value, scan_id)
            if candidate is None and missing_is_unknown and not timeline:
                candidate = _latest_range_start(self._fired_ranges.get(rung_index, []), scan_id)
            if candidate is not None and (best is None or candidate > best):
                best = candidate
        return best

    def ever_fired(self) -> set[K]:
        return {rung for rung, ranges in self._fired_ranges.items() if ranges}

    def rung_writes_at(self, rung_index: K, scan_id: int) -> PMap | None:
        if _find_scan_range(self._fired_ranges.get(rung_index, []), scan_id) is None:
            return None
        writes: dict[str, Any] = {}
        for tag_name in self._tags_by_rung.get(rung_index, ()):
            range_ = _find_value_range(
                self._value_timelines.get((rung_index, tag_name), []), scan_id
            )
            if range_ is not None:
                value = _value_at(range_, scan_id)
                if value is not _MISSING_WRITE:
                    writes[tag_name] = value
        return pmap(writes)

    def varied_on(self, rung_index: K, tag_name: str, scan_id: int) -> bool:
        """Whether this scope attempted unequal values for the tag this scan."""
        return (
            _find_scan_range(self._varied_ranges.get((rung_index, tag_name), []), scan_id)
            is not None
        )

    def value_at(self, rung_index: K, tag_name: str, scan_id: int) -> Any:
        """Final attempted value, or a private unknown/missing sentinel."""
        range_ = _find_value_range(self._value_timelines.get((rung_index, tag_name), ()), scan_id)
        return _MISSING_WRITE if range_ is None else _value_at(range_, scan_id)

    def value_or_varied_scans(
        self,
        rung_index: K,
        tag_name: str,
        value: Any,
        scan_ids: tuple[int, ...],
    ) -> tuple[int, ...]:
        """Intersect ordered scans with matching final values or varied writes.

        The retained columns are range-compressed.  Walk those ranges directly
        so a long constant non-matching interval stays O(ranges), rather than
        issuing one binary-search lookup per physical scan.  Unknown value
        ranges remain conservative and nominate every selected scan they cover.
        """

        if not scan_ids:
            return ()
        selected: set[int] = set()
        first_scan, last_scan = scan_ids[0], scan_ids[-1]

        for range_ in _expanded(
            self._value_timelines.get((rung_index, tag_name), ()), first_scan, last_scan
        ):
            lo = bisect_left(scan_ids, range_.start_scan_id)
            hi = bisect_right(scan_ids, range_.end_scan_id, lo)
            if lo == hi:
                continue
            payload = range_.payload
            if isinstance(payload, Constant):
                if payload.value is not _MISSING_WRITE and _equal(payload.value, value):
                    selected.update(scan_ids[lo:hi])
            elif isinstance(payload, Alternating):
                selected.update(
                    scan_id
                    for scan_id in scan_ids[lo:hi]
                    if (candidate := _value_at(range_, scan_id)) is not _MISSING_WRITE
                    and _equal(candidate, value)
                )
            elif isinstance(payload, Arithmetic):
                try:
                    offset = (value - payload.base) // payload.delta
                    candidate = range_.start_scan_id + offset
                except (TypeError, ValueError, ZeroDivisionError):
                    candidate = None
                candidate_index = (
                    bisect_left(scan_ids, candidate, lo, hi) if candidate is not None else hi
                )
                if (
                    candidate is not None
                    and candidate_index < hi
                    and scan_ids[candidate_index] == candidate
                    and _equal(_value_at(range_, candidate), value)
                ):
                    selected.add(scan_ids[candidate_index])
            else:
                selected.update(scan_ids[lo:hi])

        for range_ in _expanded(
            self._varied_ranges.get((rung_index, tag_name), ()), first_scan, last_scan
        ):
            lo = bisect_left(scan_ids, range_.start_scan_id)
            hi = bisect_right(scan_ids, range_.end_scan_id, lo)
            selected.update(scan_ids[lo:hi])

        return tuple(sorted(selected))

    def any_wrote_on(self, tag_name: str, scan_id: int, *, excluding: K | None = None) -> bool:
        return any(
            rung != excluding
            and _wrote_at(self._value_timelines.get((rung, tag_name), ()), scan_id)
            for rung in self._writers_by_tag.get(tag_name, ())
        )

    def varied_scans(
        self, rung_indices: frozenset[K], tag_name: str, scan_ids: tuple[int, ...]
    ) -> tuple[int, ...]:
        """Return candidate scan IDs carrying a sparse varied=true fact."""
        return tuple(
            scan_id
            for scan_id in scan_ids
            if any(self.varied_on(rung, tag_name, scan_id) for rung in rung_indices)
        )

    def write_scans(
        self, rung_indices: frozenset[K], tag_name: str, scan_ids: tuple[int, ...]
    ) -> tuple[int, ...]:
        """Intersect selected scan IDs with columnar write ranges."""
        timelines = tuple(self._value_timelines.get((rung, tag_name), ()) for rung in rung_indices)
        return tuple(
            scan_id
            for scan_id in scan_ids
            if any(_wrote_at(timeline, scan_id) for timeline in timelines)
        )

    def observed_writers_of(self, tag_name: str) -> frozenset[K]:
        return frozenset(self._writers_by_tag.get(tag_name, ()))

    def observed_writers_of_between(
        self, tag_name: str, first_scan: int, last_scan: int
    ) -> frozenset[K]:
        writers = self._writers_by_tag.get(tag_name, ())
        return frozenset(
            rung
            for rung in writers
            if _ranges_have_write(
                self._value_timelines.get((rung, tag_name), ()), first_scan, last_scan
            )
        )

    def last_tag_write_before(
        self, writer_indices: frozenset[K], tag_name: str, before_scan_id: int
    ) -> tuple[int, Any] | None:
        best: tuple[int, Any] | None = None
        for rung_index in writer_indices:
            result = _last_value_before(
                self._value_timelines.get((rung_index, tag_name), []), before_scan_id
            )
            if result is not None and (best is None or result[0] > best[0]):
                best = result
        return best

    def tag_transition_candidate_scans_before(
        self, writer_indices: frozenset[K], tag_name: str, before_scan_id: int
    ) -> tuple[int, ...]:
        candidates: set[int] = set()
        for rung_index in writer_indices:
            timeline = self._value_timelines.get((rung_index, tag_name), ())
            for range_ in _expanded(timeline, None, before_scan_id - 1):
                end = min(range_.end_scan_id, before_scan_id - 1)
                payload = range_.payload
                if isinstance(payload, Constant):
                    if payload.value is not _MISSING_WRITE:
                        candidates.add(range_.start_scan_id)
                elif isinstance(payload, Alternating):
                    candidate = _latest_write_in_range(range_, end)
                    if candidate is not None:
                        candidates.add(candidate)
                elif isinstance(payload, (Arithmetic, Unknown)):
                    candidates.add(end)
        return tuple(sorted(candidates, reverse=True))

    def value_is_known(self, rung_index: K, tag_name: str) -> bool:
        """Whether retained final values remain exact for this one column."""
        key = (rung_index, tag_name)
        return key not in self._unknown_keys and key not in self._partly_unknown

    def reset(self) -> None:
        self._fired_ranges.clear()
        self._value_timelines.clear()
        self._varied_ranges.clear()
        self._tags_by_rung.clear()
        self._writers_by_tag.clear()
        self._entries.clear()
        self._dropped.clear()
        self._floors.clear()
        self._unknown_keys.clear()
        self._partly_unknown.clear()
        self._done()

    def trim_before(self, min_scan_id: int) -> None:
        if min_scan_id <= 0:
            return
        self._trim(min_scan_id=min_scan_id)

    def trim_after(self, max_scan_id: int) -> None:
        self._trim(max_scan_id=max_scan_id)

    def _trim(self, *, min_scan_id: int | None = None, max_scan_id: int | None = None) -> None:
        assert min_scan_id is None or max_scan_id is None, "trim one end at a time"
        for rung, ranges in list(self._fired_ranges.items()):
            kept = _trim_scan_ranges(ranges, min_scan_id, max_scan_id)
            if kept:
                self._fired_ranges[rung] = kept
            else:
                self._fired_ranges.pop(rung)
        for key, ranges in list(self._value_timelines.items()):
            whole, dropped, floor = self._whole(key, ranges)
            if min_scan_id is not None:
                if min_scan_id <= ranges[0].start_scan_id:
                    continue
                # A trim from the front keeps each column's count.  Stepping and a long run
                # trim on different scans.  Each must turn the column Unknown on the same scan.
                kept = self._cut(key, whole, dropped, min_scan_id)
            else:
                assert max_scan_id is not None
                # A cut from the back counts the column again.
                kept = _as_of(_trim_value_ranges(whole, None, max_scan_id), whole, max_scan_id)
                if floor is not None:
                    kept = _trim_value_ranges(kept, floor, None)
                gone = self._unknown_keys.get(key)
                if gone is None or gone > max_scan_id:
                    self._unknown_keys.pop(key, None)  # It reached the limit after the cut.
                    held = self._dropped.get(key, 0) + _written_out_length(kept)
                    if held:
                        self._entries[key] = held
                    else:
                        self._entries.pop(key, None)
            if kept:
                self._value_timelines[key] = kept
            else:
                self._value_timelines.pop(key)
                self._partly_unknown.discard(key)
                self._floors.pop(key, None)
        for key, ranges in list(self._varied_ranges.items()):
            kept = _trim_scan_ranges(ranges, min_scan_id, max_scan_id)
            if kept:
                self._varied_ranges[key] = kept
            else:
                self._varied_ranges.pop(key)
        self._rebuild_indexes()
        self._done()

    def _whole(self, key: tuple[K, str], ranges: list[Any]) -> tuple[list[Any], int, int | None]:
        """*ranges* with the last two ranges a trim took off put back, as stepping held them.

        It also returns the entries trims took off the result, and the first scan the column
        holds.  That scan is None when nothing went back.
        """
        dropped = self._dropped.get(key, 0)
        held = self._floors.get(key)
        if held is None or not ranges or type(ranges[0]) is not ValueRange:
            return ranges, dropped, None
        floor, before = held
        if ranges[0].start_scan_id != floor:
            return ranges, dropped, None
        last = before[-1]
        if last.end_scan_id < floor:
            return [*before, *ranges], dropped - len(before), floor
        # The trim cut this range short.
        whole = ValueRange(last.start_scan_id, ranges[0].end_scan_id, last.payload)
        if not _carries(ranges[0], whole):
            return ranges, dropped, None  # Appends changed the piece the trim left.
        return [*before[:-1], whole, *ranges[1:]], dropped - len(before) + 1, floor

    def _cut(self, key: tuple[K, str], ranges: list[Any], dropped: int, floor: int) -> list[Any]:
        """*ranges* from scan *floor* on.  Each scan before it still counts.

        *dropped* is what trims took off *ranges* before.
        """
        end = bisect_right(ranges, floor - 1, key=_start_of)
        cut = _trim_value_ranges(ranges[end - 1 : end], floor, None)
        kept = [*cut, *ranges[end:]]
        taken = ranges[:end]
        self._dropped[key] = dropped + _written_out_length(taken) - _written_out_length(cut)
        before = list(islice(_expanded_reversed(taken, floor - 1), 2))[::-1]
        if kept and before:
            self._floors[key] = (kept[0].start_scan_id, before)
        else:
            self._floors.pop(key, None)
        return kept

    def carried_to(self, scan_id: int) -> RungFiringTimelines[K]:
        """An empty record that counts each column on from this one's count at *scan_id*.

        A replay steps from a checkpoint with a record of its own.  An append reads a column's
        last two ranges at most, so with those and the count it turns Unknown where this did.
        """
        carried: RungFiringTimelines[K] = RungFiringTimelines()
        for key, ranges in self._value_timelines.items():
            # The last three elements that start by *scan_id* hold its last two ranges.
            end = bisect_right(ranges, scan_id, key=_start_of)
            low = max(0, end - 3)
            if low:
                tail, dropped = ranges[low:end], self._dropped.get(key, 0)
                dropped += _written_out_length(ranges[:low])
            else:
                tail, dropped, _floor = self._whole(key, ranges[:end])
            held = _as_of(_trim_value_ranges(tail, None, scan_id), tail, scan_id)
            if not held:
                continue
            gone = self._unknown_keys.get(key)
            if gone is not None and gone <= scan_id:
                carried._unknown_keys[key] = gone
            else:
                carried._entries[key] = dropped + _written_out_length(held)
            _unroll_tail(held)
            carried._value_timelines[key] = held[-2:]
        carried._rebuild_indexes()
        return carried

    def snapshot(self, *, up_to: int | None = None) -> RungFiringTimelines[K]:
        frozen: RungFiringTimelines[K] = RungFiringTimelines()
        frozen._fired_ranges = {rung: list(ranges) for rung, ranges in self._fired_ranges.items()}
        frozen._value_timelines = {k: list(ranges) for k, ranges in self._value_timelines.items()}
        frozen._varied_ranges = {k: list(ranges) for k, ranges in self._varied_ranges.items()}
        frozen._entries = dict(self._entries)
        frozen._dropped = dict(self._dropped)
        frozen._floors = dict(self._floors)
        frozen._unknown_keys = dict(self._unknown_keys)
        frozen._partly_unknown = set(self._partly_unknown)
        if up_to is None:
            frozen._rebuild_indexes()
        else:
            frozen._trim(max_scan_id=up_to)
        frozen._done()
        return frozen

    def _rebuild_indexes(self) -> None:
        self._tags_by_rung = {}
        self._writers_by_tag = {}
        for rung, tag in self._value_timelines:
            self._tags_by_rung.setdefault(rung, set()).add(tag)
            ranges = self._value_timelines[(rung, tag)]
            if ranges and _ranges_have_write(
                ranges, ranges[0].start_scan_id, ranges[-1].end_scan_id
            ):
                self._writers_by_tag.setdefault(tag, set()).add(rung)


def _equal(left: Any, right: Any) -> bool:
    try:
        return bool(left == right)
    except Exception:
        return left is right


def _append_scan_range(ranges: list[ScanRange], scan_id: int) -> None:
    _unroll_tail(ranges)
    if ranges and ranges[-1].end_scan_id == scan_id - 1:
        last = ranges[-1]
        ranges[-1] = ScanRange(last.start_scan_id, scan_id)
    else:
        ranges.append(ScanRange(scan_id, scan_id))


def _extend_scan_range(ranges: list[ScanRange], last_scan_id: int) -> None:
    _unroll_tail(ranges)
    ranges[-1] = ScanRange(ranges[-1].start_scan_id, last_scan_id)


def _coalesced(ranges: list[Any]) -> list[Any]:
    """Join ranges that touch and say the same thing, as appending them would have."""
    out: list[Any] = []
    for range_ in ranges:
        if type(range_) is not Repeat:
            _push_joined(out, range_)
        elif out and _joins(_last_written_out(out), range_.ranges[0]):
            # Only the edges of a repeat can join a neighbour.  Its own ranges never do.
            for piece in _repetition(range_, 0):
                _push_joined(out, piece)
            out.extend(_repeats(range_, 1, range_.count - 1))
        else:
            out.append(range_)
    return out


def _push_joined(out: list[Any], range_: Any) -> None:
    if out and _joins(_last_written_out(out), range_):
        _unroll_tail(out)
        last = out[-1]
        if isinstance(range_, ScanRange):
            out[-1] = ScanRange(last.start_scan_id, range_.end_scan_id)
        else:
            out[-1] = ValueRange(last.start_scan_id, range_.end_scan_id, last.payload)
    else:
        out.append(range_)


def _joins(last: Any, range_: Any) -> bool:
    touching = last.end_scan_id == range_.start_scan_id - 1
    if isinstance(range_, ScanRange):
        return touching
    return (
        touching
        and isinstance(last, ValueRange)
        and isinstance(range_.payload, (Constant, Unknown))
        and type(range_.payload) is type(last.payload)
        and _equal(_value_at(last, last.end_scan_id), _value_at(range_, range_.start_scan_id))
    )


def _append_value(timeline: list[ValueRange], scan_id: int, value: Any) -> int:
    """Append a scalar value.  Returns how many ranges the column gained, -1 to 1."""
    _unroll_tail(timeline)
    if timeline:
        last = timeline[-1]
        adjacent = last.end_scan_id == scan_id - 1
        if adjacent and _equal(_value_at(last, scan_id), value):
            timeline[-1] = ValueRange(last.start_scan_id, scan_id, last.payload)
            return 0
    if len(timeline) >= 2:
        first, second = timeline[-2:]
        if (
            first.start_scan_id == first.end_scan_id == scan_id - 2
            and second.start_scan_id == second.end_scan_id == scan_id - 1
        ):
            v0, v1 = _value_at(first, first.start_scan_id), _value_at(second, second.start_scan_id)
            if not _equal(v0, v1) and _equal(v0, value):
                timeline[-2:] = [ValueRange(scan_id - 2, scan_id, Alternating(v0, v1))]
                return -1
            if (
                _is_arithmetic_value(v0)
                and _is_arithmetic_value(v1)
                and _is_arithmetic_value(value)
            ):
                delta = v1 - v0
                if delta != 0 and value - v1 == delta:
                    timeline[-2:] = [ValueRange(scan_id - 2, scan_id, Arithmetic(v0, delta))]
                    return -1
    payload: ValuePayload = Unknown() if value is _UNKNOWN_VALUE else Constant(value)
    timeline.append(ValueRange(scan_id, scan_id, payload))
    return 1


def _is_arithmetic_value(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _value_at(range_: ValueRange, scan_id: int) -> Any:
    payload = range_.payload
    if isinstance(payload, Constant):
        return payload.value
    if isinstance(payload, Alternating):
        return (
            payload.value_on_even
            if (scan_id - range_.start_scan_id) % 2 == 0
            else payload.value_on_odd
        )
    if isinstance(payload, Arithmetic):
        return payload.base + payload.delta * (scan_id - range_.start_scan_id)
    return _UNKNOWN_VALUE


def _find_range(ranges: Sequence[Any], scan_id: int) -> Any:
    """The written-out range holding *scan_id*, or None."""
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        range_ = ranges[mid]
        if scan_id < range_.start_scan_id:
            hi = mid
        elif scan_id > range_.end_scan_id:
            lo = mid + 1
        elif type(range_) is Repeat:
            index = (scan_id - range_.start_scan_id) // range_.period
            inner = _find_index(range_.ranges, scan_id - index * range_.period)
            if inner is None:
                return None
            step = range_.steps[inner] * index if range_.steps else 0
            return _moved(range_.ranges[inner], index * range_.period, step)
        else:
            return range_
    return None


def _find_index(ranges: Sequence[Any], scan_id: int) -> int | None:
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        range_ = ranges[mid]
        if scan_id < range_.start_scan_id:
            hi = mid
        elif scan_id > range_.end_scan_id:
            lo = mid + 1
        else:
            return mid
    return None


def _find_scan_range(ranges: Sequence[ScanRange], scan_id: int) -> ScanRange | None:
    return _find_range(ranges, scan_id)


def _find_value_range(ranges: Sequence[ValueRange], scan_id: int) -> ValueRange | None:
    return _find_range(ranges, scan_id)


def _latest_scan(ranges: list[ScanRange], scan_id: int) -> int | None:
    for range_ in _expanded_reversed(ranges, scan_id):
        return min(scan_id, range_.end_scan_id)
    return None


def _latest_range_start(ranges: list[ScanRange], scan_id: int) -> int | None:
    for range_ in _expanded_reversed(ranges, scan_id):
        return range_.start_scan_id
    return None


def _latest_value_transition(timeline: list[ValueRange], value: Any, scan_id: int) -> int | None:
    for range_ in _expanded_reversed(timeline, scan_id):
        end = min(scan_id, range_.end_scan_id)
        payload = range_.payload
        if isinstance(payload, Constant):
            if payload.value is not _MISSING_WRITE and _equal(payload.value, value):
                return range_.start_scan_id
        elif isinstance(payload, Alternating):
            for candidate in (end, end - 1):
                if candidate < range_.start_scan_id:
                    continue
                candidate_value = _value_at(range_, candidate)
                if candidate_value is _UNKNOWN_VALUE or (
                    candidate_value is not _MISSING_WRITE and _equal(candidate_value, value)
                ):
                    return candidate
        elif isinstance(payload, Arithmetic):
            try:
                offset = (value - payload.base) // payload.delta
            except (TypeError, ValueError, ZeroDivisionError):
                continue
            candidate = range_.start_scan_id + offset
            if range_.start_scan_id <= candidate <= end and _equal(
                _value_at(range_, candidate), value
            ):
                return candidate
        else:
            return range_.start_scan_id
    return None


def _last_value_before(timeline: list[ValueRange], before_scan_id: int) -> tuple[int, Any] | None:
    for range_ in _expanded_reversed(timeline, before_scan_id - 1):
        if range_.start_scan_id < before_scan_id:
            scan_id = min(range_.end_scan_id, before_scan_id - 1)
            candidate = _latest_write_in_range(range_, scan_id)
            if candidate is not None:
                return candidate, _value_at(range_, candidate)
    return None


def _wrote_at(ranges: Sequence[ValueRange], scan_id: int) -> bool:
    range_ = _find_value_range(ranges, scan_id)
    return range_ is not None and _value_at(range_, scan_id) is not _MISSING_WRITE


def _latest_write_in_range(
    range_: ValueRange, end_scan_id: int, first_scan_id: int | None = None
) -> int | None:
    lower = range_.start_scan_id if first_scan_id is None else first_scan_id
    for scan_id in (end_scan_id, end_scan_id - 1):
        if scan_id >= lower and _value_at(range_, scan_id) is not _MISSING_WRITE:
            return scan_id
    return None


def _ranges_have_write(ranges: Sequence[ValueRange], first_scan: int, last_scan: int) -> bool:
    for range_ in _expanded(ranges, first_scan, last_scan):
        overlap_start = max(range_.start_scan_id, first_scan)
        overlap_end = min(range_.end_scan_id, last_scan)
        if (
            overlap_start <= overlap_end
            and _latest_write_in_range(range_, overlap_end, overlap_start) is not None
        ):
            return True
    return False


def _replace_ranges(mine: Any, theirs: Any, trim: Any, first: int, last: int) -> None:
    """One table's scans *first*..*last* taken from *theirs*."""
    for key in set(mine) | set(theirs):
        ranges = mine.get(key, [])
        middle = trim(theirs.get(key, []), first, last)
        merged = [*trim(ranges, None, first - 1), *middle, *trim(ranges, last + 1, None)]
        if merged:
            mine[key] = _coalesced(merged)
        else:
            mine.pop(key, None)


def _trim_scan_ranges(
    ranges: list[ScanRange], min_scan: int | None, max_scan: int | None
) -> list[ScanRange]:
    kept: list[ScanRange] = []
    for range_ in ranges:
        if type(range_) is Repeat:
            kept.extend(_trim_repeat(range_, min_scan, max_scan, _trim_scan_ranges))
            continue
        start = (
            max(range_.start_scan_id, min_scan) if min_scan is not None else range_.start_scan_id
        )
        end = min(range_.end_scan_id, max_scan) if max_scan is not None else range_.end_scan_id
        if start <= end:
            kept.append(ScanRange(start, end))
    return kept


def _trim_value_ranges(
    ranges: list[ValueRange], min_scan: int | None, max_scan: int | None
) -> list[ValueRange]:
    # A settle trims each column it touches several times.  Only the elements at each end
    # can change.  The ones between go over as they are.
    low = 0 if min_scan is None else bisect_left(ranges, min_scan, key=_end_of)
    high = len(ranges) if max_scan is None else bisect_right(ranges, max_scan, key=_start_of)
    if high - low <= 2:
        return _trim_each(ranges[low:high], min_scan, max_scan)
    return [
        *_trim_each(ranges[low : low + 1], min_scan, max_scan),
        *ranges[low + 1 : high - 1],
        *_trim_each(ranges[high - 1 : high], min_scan, max_scan),
    ]


def _trim_each(
    ranges: Sequence[ValueRange], min_scan: int | None, max_scan: int | None
) -> list[ValueRange]:
    kept: list[ValueRange] = []
    for range_ in ranges:
        if type(range_) is Repeat:
            kept.extend(_trim_repeat(range_, min_scan, max_scan, _trim_value_ranges))
            continue
        start = (
            max(range_.start_scan_id, min_scan) if min_scan is not None else range_.start_scan_id
        )
        end = min(range_.end_scan_id, max_scan) if max_scan is not None else range_.end_scan_id
        if start > end:
            continue
        payload = range_.payload
        if start != range_.start_scan_id:
            offset = start - range_.start_scan_id
            if isinstance(payload, Alternating) and offset % 2:
                payload = Alternating(payload.value_on_odd, payload.value_on_even)
            elif isinstance(payload, Arithmetic):
                payload = Arithmetic(payload.base + payload.delta * offset, payload.delta)
        kept.append(ValueRange(start, end, payload))
    return kept


# --- Repeats ------------------------------------------------------------------------


def _moved(range_: Any, offset: int, step: int = 0) -> Any:
    if not offset and not step:
        return range_
    if isinstance(range_, ScanRange):
        return ScanRange(range_.start_scan_id + offset, range_.end_scan_id + offset)
    payload = range_.payload
    if step:
        if isinstance(payload, Constant):
            payload = Constant(payload.value + step)
        elif isinstance(payload, Alternating):
            payload = Alternating(payload.value_on_even + step, payload.value_on_odd + step)
        elif isinstance(payload, Arithmetic):
            payload = Arithmetic(payload.base + step, payload.delta)
    return ValueRange(range_.start_scan_id + offset, range_.end_scan_id + offset, payload)


def _repetition(repeat: Repeat, index: int) -> list[Any]:
    offset = index * repeat.period
    if not repeat.steps:
        return [_moved(range_, offset) for range_ in repeat.ranges]
    return [
        _moved(range_, offset, step * index)
        for range_, step in zip(repeat.ranges, repeat.steps, strict=True)
    ]


def _repeats(repeat: Repeat, first: int, last: int) -> list[Any]:
    """Repetitions *first*..*last* of *repeat*, as one entry.  A single repetition is written out."""
    if first > last:
        return []
    if first == last:
        return _repetition(repeat, first)
    head = _repetition(repeat, first)
    end = repeat.ranges[-1].end_scan_id + last * repeat.period
    return [
        Repeat(
            head[0].start_scan_id,
            end,
            repeat.period,
            tuple(head),
            last - first + 1,
            repeat.steps,
        )
    ]


def _overlapping(repeat: Repeat, first: int | None, last: int | None) -> tuple[int, int]:
    """The first and last repetition with a scan in *first*..*last*."""
    period = repeat.period
    lo = 0 if first is None else max(0, -((repeat.ranges[-1].end_scan_id - first) // period))
    hi = repeat.count - 1
    if last is not None:
        hi = min(hi, (last - repeat.start_scan_id) // period)
    return lo, hi


def _expanded(ranges: Sequence[Any], first: int | None, last: int | None) -> Iterator[Any]:
    """The written-out ranges with a scan in *first*..*last*, in order."""
    start = 0 if first is None else bisect_left(ranges, first, key=_end_of)
    for position in range(start, len(ranges)):
        element = ranges[position]
        if last is not None and element.start_scan_id > last:
            return
        if type(element) is not Repeat:
            yield element
            continue
        lo, hi = _overlapping(element, first, last)
        for index in range(lo, hi + 1):
            for range_ in _repetition(element, index):
                if last is not None and range_.start_scan_id > last:
                    return
                if first is None or range_.end_scan_id >= first:
                    yield range_


def _expanded_reversed(ranges: Sequence[Any], last: int | None) -> Iterator[Any]:
    """The written-out ranges that start at or before *last*, latest first."""
    stop = len(ranges) if last is None else bisect_right(ranges, last, key=_start_of)
    for position in range(stop - 1, -1, -1):
        element = ranges[position]
        if type(element) is not Repeat:
            yield element
            continue
        _lo, hi = _overlapping(element, None, last)
        for index in range(hi, -1, -1):
            for range_ in reversed(_repetition(element, index)):
                if last is None or range_.start_scan_id <= last:
                    yield range_


def _end_of(element: Any) -> int:
    return element.end_scan_id


def _start_of(element: Any) -> int:
    return element.start_scan_id


def _written_out_length(ranges: Sequence[Any]) -> int:
    return sum(
        len(element.ranges) * element.count if type(element) is Repeat else 1 for element in ranges
    )


def _last_written_out(ranges: Sequence[Any]) -> Any:
    last = ranges[-1]
    if type(last) is Repeat:
        index = last.count - 1
        step = last.steps[-1] * index if last.steps else 0
        return _moved(last.ranges[-1], index * last.period, step)
    return last


def _unroll_tail(ranges: list[Any]) -> None:
    """Write out a list's last two ranges, which an append reads and may change."""
    while True:
        if ranges and type(ranges[-1]) is Repeat:
            after: list[Any] = []
        elif len(ranges) >= 2 and type(ranges[-2]) is Repeat:
            after = [ranges.pop()]
        else:
            return
        repeat = ranges.pop()
        ranges.extend(_repeats(repeat, 0, repeat.count - 2))
        ranges.extend(_repetition(repeat, repeat.count - 1))
        ranges.extend(after)


def _trim_repeat(
    repeat: Repeat, min_scan: int | None, max_scan: int | None, trim: Any
) -> list[Any]:
    first, last = _overlapping(repeat, min_scan, max_scan)
    if first > last:
        return []
    period = repeat.period
    whole_first = first
    if min_scan is not None and repeat.start_scan_id + first * period < min_scan:
        whole_first += 1
    whole_last = last
    if max_scan is not None and repeat.ranges[-1].end_scan_id + last * period > max_scan:
        whole_last -= 1
    if whole_first > whole_last:
        pieces = [
            range_ for index in range(first, last + 1) for range_ in _repetition(repeat, index)
        ]
        return trim(pieces, min_scan, max_scan)
    kept: list[Any] = []
    if whole_first > first:
        kept += trim(_repetition(repeat, first), min_scan, max_scan)
    kept += _repeats(repeat, whole_first, whole_last)
    if whole_last < last:
        kept += trim(_repetition(repeat, last), min_scan, max_scan)
    return kept


def _same_value(left: Any, right: Any) -> bool:
    return type(left) is type(right) and _equal(left, right)


def _same_payload(left: Any, right: Any) -> bool:
    if type(left) is not type(right):
        return False
    if isinstance(left, Constant):
        return _same_value(left.value, right.value)
    if isinstance(left, Alternating):
        return _same_value(left.value_on_even, right.value_on_even) and _same_value(
            left.value_on_odd, right.value_on_odd
        )
    if isinstance(left, Arithmetic):
        return left.base == right.base and left.delta == right.delta
    return True


def _cycle_steps(
    earlier: Sequence[Any], later: Sequence[Any], period: int
) -> tuple[int, ...] | None:
    """How far each range's values moved from *earlier* to *later*, or None.

    None unless *later* is *earlier* moved one period on, range for range.  Each range must
    hold the same values, or whole numbers that all moved by one amount.
    """
    if len(earlier) != len(later):
        return None
    steps = []
    for a, b in zip(earlier, later, strict=True):
        if b.start_scan_id != a.start_scan_id + period or b.end_scan_id != a.end_scan_id + period:
            return None
        if isinstance(a, ScanRange) or _same_payload(a.payload, b.payload):
            steps.append(0)
            continue
        step = _payload_step(a.payload, b.payload)
        if step is None:
            return None
        steps.append(step)
    return tuple(steps)


def _payload_step(earlier: Any, later: Any) -> int | None:
    if type(earlier) is not type(later):
        return None
    if isinstance(earlier, Constant):
        pairs = [(earlier.value, later.value)]
    elif isinstance(earlier, Alternating):
        pairs = [
            (earlier.value_on_even, later.value_on_even),
            (earlier.value_on_odd, later.value_on_odd),
        ]
    elif isinstance(earlier, Arithmetic):
        if earlier.delta != later.delta:
            return None
        pairs = [(earlier.base, later.base)]
    else:
        return None
    if not all(_is_arithmetic_value(a) and _is_arithmetic_value(b) for a, b in pairs):
        return None
    steps = {b - a for a, b in pairs}
    return steps.pop() if len(steps) == 1 else None


def _steps_keep_their_order(cycle: Sequence[Any], steps: tuple[int, ...]) -> bool:
    """Whether every value of a column with steps moves by one amount.

    Appending compares neighbouring values.  When all of them are whole numbers that move by
    one amount, each comparison comes out the same on every cycle.  A value that stood still
    beside one that moves could meet it on some later cycle and join it.
    """
    moved = {step for step, range_ in zip(steps, cycle, strict=True) if _has_numbers(range_)}
    return len(moved) == 1 and all(
        _has_numbers(range_) or isinstance(range_.payload, Unknown) or _is_missing(range_)
        for range_ in cycle
    )


def _has_numbers(range_: Any) -> bool:
    payload = range_.payload
    if isinstance(payload, Constant):
        return _is_arithmetic_value(payload.value)
    if isinstance(payload, Alternating):
        return _is_arithmetic_value(payload.value_on_even) and _is_arithmetic_value(
            payload.value_on_odd
        )
    return isinstance(payload, Arithmetic)


def _is_missing(range_: Any) -> bool:
    payload = range_.payload
    return isinstance(payload, Constant) and payload.value is _MISSING_WRITE


_EXTEND = "extend"
_NO_LIMIT = 1 << 62


# The most repetitions a join appends one by one before it takes the rest as they are.
_REJOIN_REPETITIONS = 8


@dataclass(frozen=True, slots=True)
class _Cycle:
    """How one column takes more cycles.  See ``plan_repeat``."""

    cycle: tuple[Any, ...]
    steps: tuple[int, ...]
    step: int  # how far each of the column's whole numbers moves a cycle
    trailing: tuple[Any, ...]  # the next cycle's first scans, recorded already
    limit: int = _NO_LIMIT  # the most cycles the plan may record


def _same_range(left: Any, right: Any) -> bool:
    return (
        type(left) is ValueRange
        and type(right) is ValueRange
        and left.start_scan_id == right.start_scan_id
        and left.end_scan_id == right.end_scan_id
        and _same_payload(left.payload, right.payload)
    )


def _append_range(timeline: list[Any], range_: ValueRange) -> None:
    """Append each scan of *range_* as stepping does, without counting entries."""
    high = range_.end_scan_id
    for scan_id in range(range_.start_scan_id, high + 1):
        _append_value(timeline, scan_id, _value_at(range_, scan_id))
        if scan_id < high and _carries(timeline[-1], range_):
            tail = timeline[-1]
            timeline[-1] = ValueRange(tail.start_scan_id, high, tail.payload)
            return


def _carries(tail: Any, range_: ValueRange) -> bool:
    """Whether appending the rest of *range_* would only lengthen *tail*.

    That needs the same kind of range agreeing on the range's last two scans.  Two flip-flops
    or two counts can agree on one scan and part on the next.
    """
    high = range_.end_scan_id
    return (
        type(tail) is ValueRange
        and type(tail.payload) is type(range_.payload)
        and _equal(_value_at(tail, high), _value_at(range_, high))
        and _equal(_value_at(tail, high - 1), _value_at(range_, high - 1))
    )


def _limit_scan(ranges: Sequence[Any], held: int) -> int | None:
    """The first scan on which stepping's column reached ``_ENTRY_LIMIT`` entries, or None.

    Appending makes an alternating or arithmetic range on its third scan out of two single
    scans.  So the column holds one entry more on such a range's second scan.
    """
    for element in ranges:
        pieces: Sequence[Any] = (element,)
        if type(element) is Repeat:
            size = len(element.ranges)
            peak = max(i + 1 + _two_singles(r) for i, r in enumerate(element.ranges))
            # The first repetition that reaches the limit, or every one if none does.
            index = max(0, -((held + peak - _ENTRY_LIMIT) // size))
            if index >= element.count:
                held += element.count * size
                continue
            held += index * size
            pieces = _repetition(element, index)
        for range_ in pieces:
            if held + 1 >= _ENTRY_LIMIT:
                return range_.start_scan_id
            if _two_singles(range_) and held + 2 >= _ENTRY_LIMIT:
                return range_.start_scan_id + 1
            held += 1
    return None


def _two_singles(range_: Any) -> bool:
    return (
        isinstance(range_.payload, (Alternating, Arithmetic))
        and range_.end_scan_id > range_.start_scan_id
    )


def _as_unknown(element: Any) -> Any:
    """*element* as stepping keeps it in a column that turned Unknown.

    Each value written reads Unknown, and each scan with no write stays one.
    """
    if type(element) is Repeat:
        ranges = tuple(_as_unknown(range_) for range_ in element.ranges)
        return Repeat(
            element.start_scan_id, element.end_scan_id, element.period, ranges, element.count
        )
    payload = element.payload
    if isinstance(payload, Alternating):
        even, odd = (
            value if value is _MISSING_WRITE else _UNKNOWN_VALUE
            for value in (payload.value_on_even, payload.value_on_odd)
        )
        if even is not odd:
            return ValueRange(element.start_scan_id, element.end_scan_id, Alternating(even, odd))
    elif _is_missing(element) or isinstance(payload, Unknown):
        return element
    return ValueRange(element.start_scan_id, element.end_scan_id, Unknown())


def _as_of(prefix: list[Any], ranges: list[Any], scan_id: int) -> list[Any]:
    """*prefix*, the ranges up to *scan_id*, as appending them had left them by then.

    Appending makes an alternating or arithmetic range only on its third scan.  So a range
    cut short of three scans was single scans then.
    """
    if not prefix or type(prefix[-1]) is Repeat:
        return list(prefix)
    cut = prefix[-1]
    whole = next(_expanded(ranges, scan_id, scan_id), None)
    if (
        whole is None
        or whole.end_scan_id == cut.end_scan_id
        or not isinstance(cut.payload, (Alternating, Arithmetic))
        or cut.end_scan_id - cut.start_scan_id >= 2
    ):
        return list(prefix)
    singles: list[Any] = []
    for scan in range(cut.start_scan_id, cut.end_scan_id + 1):
        _append_value(singles, scan, _value_at(cut, scan))
    return [*prefix[:-1], *singles]


def _missing_writes(fired: list[Any], first: int, last: int | None) -> list[Any]:
    """A scan with no write for each firing in *first*..*last*.

    Stepping appends that to each column of a rung that fires without writing its tag.  A
    record made from a later scan has no column for a tag until its rung writes it.
    """
    if last is not None and first > last:
        return []
    return [_as_missing(element) for element in _trim_scan_ranges(fired, first, last)]


def _as_missing(element: Any) -> Any:
    if type(element) is Repeat:
        return Repeat(
            element.start_scan_id,
            element.end_scan_id,
            element.period,
            tuple(_as_missing(range_) for range_ in element.ranges),
            element.count,
            (),
        )
    return ValueRange(element.start_scan_id, element.end_scan_id, Constant(_MISSING_WRITE))


def _unknown_or_missing(range_: Any) -> bool:
    """Whether *range_* holds no value: ``Unknown``, no write, or a flip between the two."""
    payload = range_.payload
    if isinstance(payload, Alternating):
        return all(
            value is _UNKNOWN_VALUE or value is _MISSING_WRITE
            for value in (payload.value_on_even, payload.value_on_odd)
        )
    return isinstance(payload, Unknown) or (
        isinstance(payload, Constant) and payload.value is _MISSING_WRITE
    )


def _step_of(range_: Any, step: int) -> int:
    return step if step and _has_numbers(range_) else 0


class RepeatPlan:
    """How a timelines object takes more cycles of its last one.  See ``plan_repeat``."""

    __slots__ = ("_actions", "_period", "_timelines", "limit")

    def __init__(
        self,
        timelines: RungFiringTimelines[Any],
        actions: list[tuple[Any, list[Any], Any]],
        period: int,
    ) -> None:
        self._timelines = timelines
        self._actions = actions
        self._period = period
        self.limit = min(
            (action.limit for _key, _ranges, action in actions if action != _EXTEND),
            default=_NO_LIMIT,
        )

    def apply(self, cycles: int) -> None:
        """Record *cycles* more cycles, no more than ``limit``."""
        assert 0 < cycles <= self.limit
        period = self._period
        added = cycles * period
        for key, ranges, action in self._actions:
            if action == _EXTEND:
                _unroll_tail(ranges)
                tail = ranges[-1]
                ranges[-1] = _moved_end(tail, tail.end_scan_id + added)
                continue
            cycle, steps, step, trailing = action.cycle, action.steps, action.step, action.trailing
            _unroll_tail(ranges)
            del ranges[-(len(cycle) + len(trailing)) :]
            before = ranges[-1] if ranges else None
            end = cycle[-1].end_scan_id + added
            if (
                type(before) is Repeat
                and before.period == period
                and before.steps == steps
                and _cycle_steps(_repetition(before, before.count - 1), cycle, period)
                == (steps or (0,) * len(cycle))
            ):
                ranges[-1] = Repeat(
                    before.start_scan_id,
                    end,
                    period,
                    before.ranges,
                    before.count + 1 + cycles,
                    steps,
                )
            else:
                ranges.append(Repeat(cycle[0].start_scan_id, end, period, cycle, 1 + cycles, steps))
            moved = [_moved(range_, added, _step_of(range_, step) * cycles) for range_ in trailing]
            ranges.extend(moved)
            if key in self._timelines._entries:
                self._timelines._entries[key] += cycles * len(cycle)
        self._timelines._done()


def _moved_end(range_: Any, end: int) -> Any:
    if isinstance(range_, ScanRange):
        return ScanRange(range_.start_scan_id, end)
    return ValueRange(range_.start_scan_id, end, range_.payload)
