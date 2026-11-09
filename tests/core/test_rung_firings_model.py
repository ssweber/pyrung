"""Each way scans go into the records leaves what stepping leaves.

A model keeps every value the rung wrote, the gaps left open, the trim's floor and the scans
the loop wrote Unknown into.  Stepping's records follow from it.  Its ranges are the scans
appended from the floor, and the values' changes give the scan a column turns Unknown on.  A
state machine drives one record through each kind of change and checks it against the model
after each one.

* Every value the record knows is the value written.
* With no gap open, a column the loop wrote no Unknown into holds stepping's ranges and count.
  It turns Unknown on stepping's scan, and how() ranks stepping's candidate scans.
* While a gap is open the count may be lower, so the column turns Unknown no sooner.
* A record with a trim has no limit.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest
from hypothesis import HealthCheck, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, precondition, rule

from pyrung.core import rung_firings
from pyrung.core.rung_firings import (
    _MISSING_WRITE,
    _UNKNOWN_VALUE,
    RungFiringTimelines,
    _append_value,
    _equal,
    _expanded,
    _is_arithmetic_value,
)

TAGS = ("a", "b")
ONE = frozenset((0,))
U, M = _UNKNOWN_VALUE, _MISSING_WRITE
_ABSENT = object()

# A scan's writes, or None when the rung does not fire.
_writes = st.fixed_dictionaries({}, optional={"a": st.integers(0, 3), "b": st.booleans()})
_event = st.one_of(st.none(), _writes)


@st.composite
def _run(draw: Any, most: int = 12) -> list[dict[str, Any] | None]:
    """Scans that hold, count, flip or do anything."""
    count = draw(st.integers(1, most))
    kind = draw(st.sampled_from(("any", "held", "ramp", "flip")))
    if kind == "any":
        return draw(st.lists(_event, min_size=count, max_size=count))
    base, other = draw(st.integers(0, 3)), draw(st.integers(0, 3))
    delta = draw(st.sampled_from((1, -1, 2)))
    values = {
        "held": [base] * count,
        "ramp": [base + delta * k for k in range(count)],
        "flip": [base if k % 2 == 0 else other for k in range(count)],
    }[kind]
    b = draw(st.one_of(st.none(), st.booleans()))
    return [{"a": value} if b is None else {"a": value, "b": b} for value in values]


def _kind(value: Any, one: Any, two: Any) -> str | None:
    if one is _ABSENT:
        return None
    if _equal(value, one):
        return "held"
    if two is _ABSENT:
        return None
    if _equal(value, two):
        return "flip"
    if all(map(_is_arithmetic_value, (value, one, two))) and value - one == one - two:
        return "count"
    return None


def _changes(column: dict[int, Any], scan: int) -> int:
    """1 unless a scan holds the value before it, or flips or counts as the scan before it did."""
    one, two, three = (column.get(scan - k, _ABSENT) for k in (1, 2, 3))
    kind = _kind(column[scan], one, two)
    return int(kind != "held" and (kind is None or _kind(one, two, three) != kind))


class Model:
    """What stepping saw, and what it left in its records."""

    def __init__(self, limit: int | None) -> None:
        self.limit = limit
        self.writes: dict[int, dict[str, Any] | None] = {}
        self.tip = 0
        self.floor = 1
        self.gaps: list[tuple[int, int]] = []
        self.loop: dict[str, set[int]] = {tag: set() for tag in TAGS}

    def add(self, writes: dict[str, Any] | None) -> int:
        self.tip += 1
        self.writes[self.tip] = writes
        return self.tip

    def in_gap(self, scan: int) -> bool:
        return any(first <= scan <= last for first, last in self.gaps)

    def column(self, tag: str) -> dict[int, Any]:
        """Each scan stepping's column holds, to its value or no write.

        A column begins on the rung's first write from the floor on, and holds each firing
        after it.
        """
        out: dict[int, Any] = {}
        for scan in range(self.floor, self.tip + 1):
            writes = self.writes[scan]
            if writes is not None and (tag in writes or out):
                out[scan] = writes.get(tag, M)
        return out

    def crossing(self, tag: str) -> tuple[int | None, int]:
        """The scan the column turns Unknown on, and its count up to then."""
        if self.limit is None:
            return None, 0
        column = self.column(tag)
        count = 0
        for scan in sorted(column):
            count += _changes(column, scan)
            if count >= self.limit:
                return scan, count
        return None, count

    def ranges(self, tag: str) -> list[Any]:
        crossing, _count = self.crossing(tag)
        timeline: list[Any] = []
        for scan, value in sorted(self.column(tag).items()):
            if crossing is not None and scan > crossing and value is not M:
                value = U
            _append_value(timeline, scan, value)
        return timeline


def _spelled(value: Any) -> Any:
    if value is U:
        return "Unknown"
    if value is M:
        return "no write"
    return (type(value).__name__, value)


def _shape(ranges: list[Any]) -> list[Any]:
    return [
        (
            r.start_scan_id,
            r.end_scan_id,
            type(r.payload).__name__,
            tuple(_spelled(v) for v in vars_of(r.payload)),
        )
        for r in ranges
    ]


def vars_of(payload: Any) -> tuple[Any, ...]:
    return tuple(getattr(payload, name) for name in payload.__slots__)


class Records(RuleBasedStateMachine):
    limit: int | None = 1_000
    trims = False

    def __init__(self) -> None:
        super().__init__()
        self.model = Model(self.limit)
        self.ours: RungFiringTimelines = RungFiringTimelines(capped=self.limit is not None)

    # --- the ways scans go in --------------------------------------------------------

    def _step(self, writes: dict[str, Any] | None) -> None:
        scan = self.model.add(writes)
        if writes is not None:
            self.ours.append(0, scan, writes)

    @rule(run=_run())
    def step(self, run: list[dict[str, Any] | None]) -> None:
        for writes in run:
            self._step(writes)

    @rule(
        length=st.integers(1, 40),
        base=st.integers(0, 3),
        delta=st.sampled_from((0, 0, 1, -1)),
        b=st.one_of(st.none(), st.booleans()),
        whole=st.booleans(),
    )
    def bulk(self, length: int, base: int, delta: int, b: bool | None, whole: bool) -> None:
        """A run of scans the loop recorded at once, as a long run does."""
        first = self.model.tip + 1
        writes: dict[str, Any] = {"a": base} if b is None else {"a": base, "b": b}
        for k in range(length):
            self.model.add({**writes, "a": base + delta * k})
        deltas = {"a": delta} if delta else None
        if whole:
            self.ours.append_repeated(0, first, length, writes, deltas=deltas)
            return
        last = first + length - 1
        for tag in self.ours.tags_of(0):
            if tag not in writes:
                self.ours.append_tag_run(0, tag, first, last, M)
        self.ours.append_fired_run(0, first, last)
        for tag, value in writes.items():
            self.ours.append_tag_run(0, tag, first, last, value, delta if tag == "a" else None)

    @rule(
        period=st.integers(1, 4),
        template=st.lists(_event, min_size=4, max_size=4),
        step=st.sampled_from((0, 0, 1, -1, 2)),
        cycles=st.integers(1, 40),
    )
    def jump(self, period: int, template: list[Any], step: int, cycles: int) -> None:
        """Three cycles stepped, then a plan records the rest at once, as a jump does."""

        def scan_of(cycle: int, index: int) -> dict[str, Any] | None:
            writes = template[index]
            if writes is None:
                return None
            return {tag: v + cycle * step if tag == "a" else v for tag, v in writes.items()}

        for cycle in range(3):
            for index in range(period):
                self._step(scan_of(cycle, index))
        plan = self.ours.plan_repeat(self.model.tip, period)
        jumped = 0 if plan is None else min(plan.limit, cycles)
        if plan is not None and jumped:
            plan.apply(jumped)
        for cycle in range(3, 3 + cycles):
            for index in range(period):
                if cycle < 3 + jumped:
                    self.model.add(scan_of(cycle, index))
                else:
                    self._step(scan_of(cycle, index))

    @rule(run=_run(30))
    def open_gap(self, run: list[dict[str, Any] | None]) -> None:
        """Scans that ran with no records, as a compiled run leaves them."""
        first = self.model.tip + 1
        for writes in run:
            self.model.add(writes)
        last = self.model.tip
        gaps = self.model.gaps
        if gaps and gaps[-1][1] == first - 1:
            gaps[-1] = (gaps[-1][0], last)
        else:
            gaps.append((first, last))
        self.ours.note_gap(first, last)

    @precondition(lambda self: self.model.gaps)
    @rule(data=st.data())
    def settle(self, data: Any) -> None:
        """Some scans of a gap stepped again from a checkpoint before them."""
        model = self.model
        start, end = data.draw(st.sampled_from(model.gaps))
        first = data.draw(st.integers(start, end))
        last = data.draw(st.integers(first, end))
        anchor = data.draw(st.integers(max(model.floor - 1, 0), first - 1))
        replay = RungFiringTimelines(capped=False)
        for scan in range(anchor + 1, last + 1):
            writes = model.writes[scan]
            if writes is not None:
                replay.append(0, scan, writes)
        self.ours.replace_range(replay, first, last)
        kept = []
        for low, high in model.gaps:
            if high < first or low > last:
                kept.append((low, high))
                continue
            if low < first:
                kept.append((low, first - 1))
            if high > last:
                kept.append((last + 1, high))
        model.gaps = kept

    @precondition(lambda self: self.trims and self.model.tip > self.model.floor)
    @rule(data=st.data())
    def front_trim(self, data: Any) -> None:
        model = self.model
        floor = data.draw(st.integers(model.floor + 1, model.tip))
        self.ours.trim_before(floor)
        model.floor = floor
        model.gaps = [(max(first, floor), last) for first, last in model.gaps if last >= floor]

    def _cut(self, cut: int) -> None:
        model = self.model
        for scan in range(cut + 1, model.tip + 1):
            del model.writes[scan]
        model.tip = cut
        model.gaps = [(first, min(last, cut)) for first, last in model.gaps if first <= cut]
        for scans in model.loop.values():
            scans.difference_update(range(cut + 1, cut + 10_000))

    @precondition(lambda self: self.model.tip > self.model.floor)
    @rule(data=st.data())
    def back_cut(self, data: Any) -> None:
        cut = data.draw(st.integers(self.model.floor, self.model.tip - 1))
        self.ours.trim_after(cut)
        self._cut(cut)

    @precondition(lambda self: self.model.tip > self.model.floor)
    @rule(data=st.data())
    def fork(self, data: Any) -> None:
        cut = data.draw(st.integers(self.model.floor, self.model.tip - 1))
        self.ours = self.ours.snapshot(up_to=cut)
        self._cut(cut)

    @rule(length=st.integers(1, 8), values=st.lists(st.integers(0, 3), min_size=8, max_size=8))
    def loop_unknown(self, length: int, values: list[int]) -> None:
        """A run the loop could not speak for.  It reads Unknown, whatever ran."""
        first = self.model.tip + 1
        for k in range(length):
            self.model.loop["a"].add(self.model.add({"a": values[k]}))
        self.ours.append_repeated(0, first, length, {"a": 0}, unknown={"a"})

    # --- what the records must say ---------------------------------------------------

    @invariant()
    def records_agree(self) -> None:
        model, ours = self.model, self.ours
        fired = {
            scan
            for scan in range(model.floor, model.tip + 1)
            if model.writes.get(scan) is not None and not model.in_gap(scan)
        }
        held = {
            scan
            for r in _expanded(ours._fired_ranges.get(0, ()), model.floor, model.tip)
            for scan in range(max(r.start_scan_id, model.floor), r.end_scan_id + 1)
            if not model.in_gap(scan)
        }
        assert held == fired
        for tag in TAGS:
            self._column_agrees(tag)

    def _column_agrees(self, tag: str) -> None:
        model, ours = self.model, self.ours
        key = (0, tag)
        column = model.column(tag)
        crossing, count = model.crossing(tag)
        theirs = ours._unknown_keys.get(key)
        loop = {scan for scan in model.loop[tag] if scan <= model.tip}
        for scan in range(model.floor, model.tip + 2):
            if model.in_gap(scan):
                continue
            mine = ours.value_at(0, tag, scan)
            if mine is U:
                assert scan in loop or (theirs is not None and scan > theirs), scan
            else:
                assert _spelled(mine) == _spelled(column.get(scan, M)), scan
        if model.loop[tag]:
            return  # The loop's Unknown counts as it reads, so only known values must agree.
        held = ours._counts.get(key, 0)
        if model.gaps:
            # The count may be low, never high.
            assert theirs is None or (crossing is not None and theirs >= crossing)
            assert theirs is not None or held <= count
            return
        assert theirs == crossing
        assert theirs is not None or held == count
        expected = model.ranges(tag)
        timeline = list(_expanded(ours._value_timelines.get(key, []), None, None))
        assert _shape(timeline) == _shape(expected)
        stepped: RungFiringTimelines = RungFiringTimelines()
        stepped._value_timelines[key] = expected
        asked = {model.floor, model.tip + 1}
        for r in expected:
            asked.update(range(r.start_scan_id - 1, r.start_scan_id + 2))
            asked.update(range(r.end_scan_id, r.end_scan_id + 2))
        for before in sorted(asked):
            assert ours.tag_transition_candidate_scans_before(
                ONE, tag, before
            ) == stepped.tag_transition_candidate_scans_before(ONE, tag, before), before


def _totals_hold(timelines: RungFiringTimelines) -> None:
    """A record's counts are the model's count of the values it holds.

    A scan that looks back into an open gap counts no change, so the count may be lower then.
    """
    gaps = timelines._gaps
    assert all(a[1] < b[0] - 1 for a, b in zip(gaps, gaps[1:], strict=False)), gaps
    for key, crossing in timelines._unknown_keys.items():
        for r in _expanded(timelines._value_timelines.get(key, ()), crossing + 1, None):
            for scan in range(max(r.start_scan_id, crossing + 1), r.end_scan_id + 1):
                assert rung_firings._value_at(r, scan) in (U, M), (key, scan)
    if timelines._limit is None:
        assert not timelines._counts and not timelines._unknown_keys
        return
    for key, ranges in timelines._value_timelines.items():
        crossing = timelines._unknown_keys.get(key)
        column = {
            scan: rung_firings._value_at(r, scan)
            for r in _expanded(ranges, None, crossing)
            for scan in range(r.start_scan_id, r.end_scan_id + 1)
            if crossing is None or scan <= crossing
        }
        changes = sum(_changes(column, scan) for scan in column)
        held = timelines._limit if crossing is not None else timelines._counts.get(key, 0)
        assert held <= changes if gaps else held == changes, (key, changes, held)


@pytest.fixture
def checked() -> Iterator[None]:
    RungFiringTimelines._check = _totals_hold
    try:
        yield
    finally:
        RungFiringTimelines._check = None


def _run_machine(
    machine: type[Records], monkeypatch: pytest.MonkeyPatch, examples: int, derandomize: bool
) -> None:
    if machine.limit is not None:
        monkeypatch.setattr(rung_firings, "_ENTRY_LIMIT", machine.limit)
    machine.TestCase.settings = settings(
        max_examples=examples,
        stateful_step_count=30,
        deadline=None,
        derandomize=derandomize,
        suppress_health_check=list(HealthCheck),
    )
    machine.TestCase().runTest()


class NoTrims(Records):
    """No column reaches its limit, and nothing trims from the front."""


class Capped(Records):
    """Columns reach a limit of a few changes, and nothing trims from the front."""

    limit = 6


class Trims(Records):
    """A history window trims from the front, so the record has no limit."""

    limit = None
    trims = True


MACHINES = [NoTrims, Capped, Trims]


@pytest.mark.parametrize("machine", MACHINES)
def test_the_records_hold_what_stepping_holds(
    machine: type[Records], monkeypatch: pytest.MonkeyPatch, checked: None
) -> None:
    _run_machine(machine, monkeypatch, 60, derandomize=True)


@pytest.mark.hypothesis
@pytest.mark.parametrize("machine", MACHINES)
def test_the_records_hold_what_stepping_holds_searched(
    machine: type[Records], monkeypatch: pytest.MonkeyPatch, checked: None
) -> None:
    _run_machine(machine, monkeypatch, 100, derandomize=False)
