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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Generic, TypeVar

from pyrsistent import PMap, pmap

K = TypeVar("K")

_VALUE_VARIETY_THRESHOLD = 100
_TRACK_DISTINCT_AFTER = 8
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


class RungFiringTimelines(Generic[K]):
    """Append-only columnar firing index, generic over rung identity."""

    __slots__ = (
        "_fired_ranges",
        "_value_timelines",
        "_varied_ranges",
        "_tags_by_rung",
        "_writers_by_tag",
        "_distinct_values",
        "_unknown_keys",
    )

    def __init__(self) -> None:
        self._fired_ranges: dict[K, list[ScanRange]] = {}
        self._value_timelines: dict[tuple[K, str], list[ValueRange]] = {}
        self._varied_ranges: dict[tuple[K, str], list[ScanRange]] = {}
        self._tags_by_rung: dict[K, set[str]] = {}
        self._writers_by_tag: dict[str, set[K]] = {}
        # Allocated only for fragmented columns. Stable, alternating, and
        # arithmetic columns pay nothing for cardinality bookkeeping.
        self._distinct_values: dict[tuple[K, str], list[Any]] = {}
        self._unknown_keys: set[tuple[K, str]] = set()

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
                _append_value(
                    self._value_timelines[(rung_index, tag_name)],
                    scan_id,
                    _MISSING_WRITE,
                )
        for tag_name, value in writes.items():
            key = (rung_index, tag_name)
            if tag_name not in tags:
                tags.add(tag_name)
            self._writers_by_tag.setdefault(tag_name, set()).add(rung_index)
            timeline = self._value_timelines.setdefault(key, [])
            if key in self._unknown_keys:
                _append_unknown(timeline, scan_id)
            else:
                increased = _append_value(timeline, scan_id, value)
                if increased and len(timeline) >= _TRACK_DISTINCT_AFTER:
                    distinct = self._distinct_values.get(key)
                    if distinct is None:
                        distinct = _known_distinct_values(timeline)
                        self._distinct_values[key] = distinct
                    elif not any(_equal(value, prior) for prior in distinct):
                        distinct.append(value)
                    if len(distinct) >= _VALUE_VARIETY_THRESHOLD:
                        self._unknown_keys.add(key)
                        self._distinct_values.pop(key, None)
            if tag_name in varied:
                _append_scan_range(self._varied_ranges.setdefault(key, []), scan_id)

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

        for range_ in self._value_timelines.get((rung_index, tag_name), ()):
            if range_.end_scan_id < first_scan:
                continue
            if range_.start_scan_id > last_scan:
                break
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

        for range_ in self._varied_ranges.get((rung_index, tag_name), ()):
            if range_.end_scan_id < first_scan:
                continue
            if range_.start_scan_id > last_scan:
                break
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
            for range_ in self._value_timelines.get((rung_index, tag_name), ()):
                if range_.start_scan_id >= before_scan_id:
                    break
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
        return (rung_index, tag_name) not in self._unknown_keys

    def reset(self) -> None:
        self._fired_ranges.clear()
        self._value_timelines.clear()
        self._varied_ranges.clear()
        self._tags_by_rung.clear()
        self._writers_by_tag.clear()
        self._distinct_values.clear()
        self._unknown_keys.clear()

    def trim_before(self, min_scan_id: int) -> None:
        if min_scan_id <= 0:
            return
        self._trim(min_scan_id=min_scan_id)

    def trim_after(self, max_scan_id: int) -> None:
        self._trim(max_scan_id=max_scan_id)

    def _trim(self, *, min_scan_id: int | None = None, max_scan_id: int | None = None) -> None:
        for rung, ranges in list(self._fired_ranges.items()):
            kept = _trim_scan_ranges(ranges, min_scan_id, max_scan_id)
            if kept:
                self._fired_ranges[rung] = kept
            else:
                self._fired_ranges.pop(rung)
        for key, ranges in list(self._value_timelines.items()):
            kept = _trim_value_ranges(ranges, min_scan_id, max_scan_id)
            if kept:
                self._value_timelines[key] = kept
            else:
                self._value_timelines.pop(key)
                self._distinct_values.pop(key, None)
                self._unknown_keys.discard(key)
        for key, ranges in list(self._varied_ranges.items()):
            kept = _trim_scan_ranges(ranges, min_scan_id, max_scan_id)
            if kept:
                self._varied_ranges[key] = kept
            else:
                self._varied_ranges.pop(key)
        self._rebuild_indexes()

    def snapshot(self, *, up_to: int | None = None) -> RungFiringTimelines[K]:
        frozen: RungFiringTimelines[K] = RungFiringTimelines()
        frozen._fired_ranges = {
            rung: _trim_scan_ranges(ranges, None, up_to)
            for rung, ranges in self._fired_ranges.items()
        }
        frozen._fired_ranges = {k: v for k, v in frozen._fired_ranges.items() if v}
        frozen._value_timelines = {
            key: _trim_value_ranges(ranges, None, up_to)
            for key, ranges in self._value_timelines.items()
        }
        frozen._value_timelines = {k: v for k, v in frozen._value_timelines.items() if v}
        frozen._varied_ranges = {
            key: _trim_scan_ranges(ranges, None, up_to)
            for key, ranges in self._varied_ranges.items()
        }
        frozen._varied_ranges = {k: v for k, v in frozen._varied_ranges.items() if v}
        frozen._distinct_values = {
            key: list(values)
            for key, values in self._distinct_values.items()
            if key in frozen._value_timelines
        }
        frozen._unknown_keys = self._unknown_keys & frozen._value_timelines.keys()
        frozen._rebuild_indexes()
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
    if ranges and ranges[-1].end_scan_id == scan_id - 1:
        last = ranges[-1]
        ranges[-1] = ScanRange(last.start_scan_id, scan_id)
    else:
        ranges.append(ScanRange(scan_id, scan_id))


def _append_unknown(timeline: list[ValueRange], scan_id: int) -> None:
    _append_value(timeline, scan_id, _UNKNOWN_VALUE)


def _append_value(timeline: list[ValueRange], scan_id: int, value: Any) -> bool:
    """Append a scalar value; return whether complexity increased."""
    if timeline:
        last = timeline[-1]
        adjacent = last.end_scan_id == scan_id - 1
        if adjacent and _equal(_value_at(last, scan_id), value):
            timeline[-1] = ValueRange(last.start_scan_id, scan_id, last.payload)
            return False
    if len(timeline) >= 2:
        first, second = timeline[-2:]
        if (
            first.start_scan_id == first.end_scan_id == scan_id - 2
            and second.start_scan_id == second.end_scan_id == scan_id - 1
        ):
            v0, v1 = _value_at(first, first.start_scan_id), _value_at(second, second.start_scan_id)
            if not _equal(v0, v1) and _equal(v0, value):
                timeline[-2:] = [ValueRange(scan_id - 2, scan_id, Alternating(v0, v1))]
                return False
            if (
                _is_arithmetic_value(v0)
                and _is_arithmetic_value(v1)
                and _is_arithmetic_value(value)
            ):
                delta = v1 - v0
                if delta != 0 and value - v1 == delta:
                    timeline[-2:] = [ValueRange(scan_id - 2, scan_id, Arithmetic(v0, delta))]
                    return False
    same_as_previous = bool(
        timeline and _equal(_value_at(timeline[-1], timeline[-1].end_scan_id), value)
    )
    payload: ValuePayload = Unknown() if value is _UNKNOWN_VALUE else Constant(value)
    timeline.append(ValueRange(scan_id, scan_id, payload))
    return not same_as_previous


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


def _find_scan_range(ranges: Sequence[ScanRange], scan_id: int) -> ScanRange | None:
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        range_ = ranges[mid]
        if scan_id < range_.start_scan_id:
            hi = mid
        elif scan_id > range_.end_scan_id:
            lo = mid + 1
        else:
            return range_
    return None


def _find_value_range(ranges: Sequence[ValueRange], scan_id: int) -> ValueRange | None:
    lo, hi = 0, len(ranges)
    while lo < hi:
        mid = (lo + hi) // 2
        range_ = ranges[mid]
        if scan_id < range_.start_scan_id:
            hi = mid
        elif scan_id > range_.end_scan_id:
            lo = mid + 1
        else:
            return range_
    return None


def _latest_scan(ranges: list[ScanRange], scan_id: int) -> int | None:
    for range_ in reversed(ranges):
        if range_.start_scan_id <= scan_id:
            return min(scan_id, range_.end_scan_id)
    return None


def _latest_range_start(ranges: list[ScanRange], scan_id: int) -> int | None:
    for range_ in reversed(ranges):
        if range_.start_scan_id <= scan_id:
            return range_.start_scan_id
    return None


def _latest_value_transition(timeline: list[ValueRange], value: Any, scan_id: int) -> int | None:
    for range_ in reversed(timeline):
        if range_.start_scan_id > scan_id:
            continue
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
    for range_ in reversed(timeline):
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
    for range_ in ranges:
        overlap_start = max(range_.start_scan_id, first_scan)
        overlap_end = min(range_.end_scan_id, last_scan)
        if (
            overlap_start <= overlap_end
            and _latest_write_in_range(range_, overlap_end, overlap_start) is not None
        ):
            return True
    return False


def _known_distinct_values(timeline: Sequence[ValueRange]) -> list[Any]:
    """Known scalar values in a fragmented column; missing is not a value."""
    distinct: list[Any] = []
    for range_ in timeline:
        payload = range_.payload
        if isinstance(payload, Constant):
            candidates = (payload.value,)
        elif isinstance(payload, Alternating):
            candidates = (payload.value_on_even, payload.value_on_odd)
        else:
            # Arithmetic is already compact, and Unknown has no exact value.
            continue
        for value in candidates:
            if value is _MISSING_WRITE or value is _UNKNOWN_VALUE:
                continue
            if not any(_equal(value, prior) for prior in distinct):
                distinct.append(value)
    return distinct


def _trim_scan_ranges(
    ranges: list[ScanRange], min_scan: int | None, max_scan: int | None
) -> list[ScanRange]:
    kept: list[ScanRange] = []
    for range_ in ranges:
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
    kept: list[ValueRange] = []
    for range_ in ranges:
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
