"""A run recorded as a repeat reads back as the same run recorded scan by scan."""

from __future__ import annotations

import ast
import random
import tracemalloc
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.core.rung_firings import _UNKNOWN_VALUE, Repeat, RungFiringTimelines

RUNGS = (0, 1, 2)
TAGS = ("a", "b")
POOL = (0, 1, True, 2.5, "x")

# One scan: rung -> (writes, varied)
Scan = dict[int, tuple[dict[str, Any], frozenset[str]]]


def _random_scan(rng: random.Random) -> Scan:
    scan: Scan = {}
    for rung in RUNGS:
        if rng.random() < 0.6:
            writes = {tag: rng.choice(POOL) for tag in TAGS if rng.random() < 0.7}
            varied = frozenset(tag for tag in writes if rng.random() < 0.2)
            scan[rung] = (writes, varied)
    return scan


def _record(timelines: RungFiringTimelines, scan_id: int, scan: Scan) -> None:
    for rung, (writes, varied) in scan.items():
        timelines.append(rung, scan_id, writes, varied)


def _shown(value: Any) -> str:
    return f"{type(value).__name__}:{value!r}" if type(value) is not object else "sentinel"


def _reads(
    timelines: RungFiringTimelines, last: int, shown: Callable[[Any], Any] = _shown
) -> dict[str, Any]:
    """What every reader of the timelines answers, over every scan."""
    scans = tuple(range(0, last + 2))
    every = frozenset(RUNGS)
    out: dict[str, Any] = {
        "ever_fired": sorted(timelines.ever_fired()),
        "tags_of": {rung: sorted(timelines.tags_of(rung)) for rung in RUNGS},
        "known": {(r, t): timelines.value_is_known(r, t) for r in RUNGS for t in TAGS},
        "writers": {tag: sorted(timelines.observed_writers_of(tag)) for tag in TAGS},
    }
    for scan_id in scans:
        out[f"at {scan_id}"] = {
            rung: sorted((tag, shown(value)) for tag, value in writes.items())
            for rung, writes in timelines.at(scan_id).items()
        }
        out[f"fired_on {scan_id}"] = sorted(timelines.fired_on(scan_id))
        out[f"latest firing {scan_id}"] = timelines.latest_firing_scan_at_or_before(every, scan_id)
        out[f"throughout {scan_id}"] = [
            sorted(timelines.fired_throughout(scan_id, scan_id + width)) for width in range(4)
        ]
        for tag in TAGS:
            out[f"any wrote {tag} {scan_id}"] = timelines.any_wrote_on(tag, scan_id)
            out[f"writers {tag} {scan_id}"] = sorted(
                timelines.observed_writers_of_between(tag, max(0, scan_id - 3), scan_id)
            )
            for rung in RUNGS:
                one = frozenset((rung,))
                key = f"{rung} {tag} {scan_id}"
                out[f"value {key}"] = shown(timelines.value_at(rung, tag, scan_id))
                out[f"varied {key}"] = timelines.varied_on(rung, tag, scan_id)
                last_write = timelines.last_tag_write_before(one, tag, scan_id)
                out[f"last write {key}"] = (
                    None if last_write is None else (last_write[0], shown(last_write[1]))
                )
                out[f"candidates {key}"] = timelines.tag_transition_candidate_scans_before(
                    one, tag, scan_id
                )
                for value in POOL:
                    for missing in (True, False):
                        out[f"transition {key} {value!r} {missing}"] = (
                            timelines.latest_value_transition_scan_at_or_before(
                                one, tag, value, scan_id, missing_is_unknown=missing
                            )
                        )
    for tag in TAGS:
        for rung in RUNGS:
            one = frozenset((rung,))
            out[f"varied scans {rung} {tag}"] = timelines.varied_scans(one, tag, scans)
            out[f"write scans {rung} {tag}"] = timelines.write_scans(one, tag, scans)
            for value in POOL:
                out[f"value or varied {rung} {tag} {value!r}"] = timelines.value_or_varied_scans(
                    rung, tag, value, scans
                )
                out[f"value or varied odd {rung} {tag} {value!r}"] = (
                    timelines.value_or_varied_scans(rung, tag, value, scans[1::3])
                )
    return out


def _recorded_both_ways(
    prefix: list[Scan],
    cycle: list[Scan] | Callable[[int], list[Scan]],
    cycles: int,
    suffix: list[Scan],
) -> tuple[RungFiringTimelines, RungFiringTimelines, bool, int]:
    """The run scan by scan, and with all but three cycles as one repeat if it can be.

    *cycle* gives the scans of each cycle, or of cycle r when it is a function.
    """

    def cycle_at(r: int) -> list[Scan]:
        return cycle if isinstance(cycle, list) else cycle(r)

    period = len(cycle_at(0))
    scans = [*prefix, *(scan for r in range(cycles) for scan in cycle_at(r)), *suffix]
    stepped: RungFiringTimelines = RungFiringTimelines()
    for scan_id, scan in enumerate(scans, start=1):
        _record(stepped, scan_id, scan)
    repeated: RungFiringTimelines = RungFiringTimelines()
    warm = len(prefix) + 3 * period
    for scan_id, scan in enumerate(scans[:warm], start=1):
        _record(repeated, scan_id, scan)
    plan = repeated.plan_repeat(warm, period)
    first = warm + 1
    if plan is not None:
        added = cycles - 3
        if added > 0:
            plan.apply(added)
            first += added * period
    for scan_id, scan in enumerate(scans[first - 1 :], start=first):
        _record(repeated, scan_id, scan)
    return stepped, repeated, plan is not None, len(scans)


def _assert_same(stepped: RungFiringTimelines, repeated: RungFiringTimelines, last: int) -> None:
    ours, theirs = _reads(repeated, last), _reads(stepped, last)
    for key in theirs:
        assert ours[key] == theirs[key], key
    assert repeated._unknown_keys == stepped._unknown_keys


class _Uncapped(RungFiringTimelines):
    """Stepping that keeps every value, where stepping stops at a hundred to save memory."""

    __slots__ = ()

    def _note_new_value(self, key: Any, timeline: Any, value: Any) -> None:
        return None


def _loose(value: Any) -> Any:
    """A value as ``==`` compares it.  A tag of 0 and False may read back either."""
    return "sentinel" if type(value) is object else value


def _assert_true_to(
    stepped: RungFiringTimelines, truth: RungFiringTimelines, ours: RungFiringTimelines, last: int
) -> None:
    """Each value ours knows is the value written, and ours says Unknown only where stepping does.

    *truth* is stepping that keeps every value.  A column that holds no Unknown answers every
    other read as one of the two answers it.  A range may start on a different scan, so the
    candidate scans may differ.
    """
    holes: set[str] = set()
    for rung in RUNGS:
        for tag in TAGS:
            for scan_id in range(last + 2):
                mine = ours.value_at(rung, tag, scan_id)
                where = (rung, tag, scan_id)
                if mine is _UNKNOWN_VALUE:
                    assert stepped.value_at(rung, tag, scan_id) is _UNKNOWN_VALUE, where
                    holes.add(f" {rung} {tag} ")
                else:
                    assert _loose(mine) == _loose(truth.value_at(rung, tag, scan_id)), where
    capped, kept = _flat(_reads(stepped, last, _loose)), _flat(_reads(truth, last, _loose))
    for key, read in _flat(_reads(ours, last, _loose)).items():
        if key.startswith("candidates ") or any(hole in f"{key} " for hole in holes):
            continue
        if read in (kept.get(key), capped.get(key)):
            continue
        assert key.startswith("transition "), key
        # Stepping may name a later scan of the same run of the value.
        _word, rung, tag, _scan, shown, _missing = key.split(" ")
        value = ast.literal_eval(shown)
        low, high = sorted((read, kept[key]))
        assert all(
            _loose(truth.value_at(int(rung), tag, scan_id)) == value
            for scan_id in range(low, high + 1)
        ), key


def _flat(reads: dict[str, Any]) -> dict[str, Any]:
    """*reads* with one entry for each column of the reads that cover them all."""
    out: dict[str, Any] = {}
    for key, read in reads.items():
        if key == "known":
            out.update({f"known {rung} {tag}": known for (rung, tag), known in read.items()})
        elif key.startswith("at "):
            for rung, writes in read.items():
                out[f"{key} {rung}"] = [tag for tag, _value in writes]
                out.update({f"{key} {rung} {tag}": value for tag, value in writes})
        else:
            out[key] = read
    return out


def _holds_a_repeat(timelines: RungFiringTimelines) -> bool:
    tables = (timelines._fired_ranges, timelines._value_timelines, timelines._varied_ranges)
    return any(
        type(element) is Repeat
        for table in tables
        for ranges in table.values()
        for element in ranges
    )


def _flip_flop() -> tuple[list[Scan], list[Scan]]:
    """Rung 0 fires every scan and alternates a value, rung 1 fires on one phase only."""
    prefix: list[Scan] = [{0: ({"a": 0}, frozenset())}]
    cycle: list[Scan] = [
        {0: ({"a": True, "b": 1}, frozenset()), 1: ({"a": 1}, frozenset({"a"}))},
        {0: ({"a": False, "b": 1}, frozenset()), 2: ({"b": "x"}, frozenset())},
    ]
    return prefix, cycle


def test_a_flip_flop_goes_in_as_a_repeat_and_reads_back_as_scan_by_scan() -> None:
    prefix, cycle = _flip_flop()
    stepped, repeated, planned, last = _recorded_both_ways(prefix, cycle, 400, [])
    assert planned
    assert _holds_a_repeat(repeated)
    assert repeated.run_count() < 20 < stepped.run_count()
    _assert_same(stepped, repeated, last)


def test_scans_after_a_repeat_and_a_trim_read_back_as_scan_by_scan() -> None:
    prefix, cycle = _flip_flop()
    suffix: list[Scan] = [cycle[0], cycle[0], {1: ({"a": 2}, frozenset())}, cycle[1]]
    stepped, repeated, planned, last = _recorded_both_ways(prefix, cycle, 40, suffix)
    assert planned
    _assert_same(stepped, repeated, last)
    for cut in (1, 6, 7, 30, 61, last - 2):
        _assert_same(stepped.snapshot(up_to=cut), repeated.snapshot(up_to=cut), last)
        early, later = stepped.snapshot(), repeated.snapshot()
        early.trim_before(cut)
        later.trim_before(cut)
        _assert_same(early, later, last)


def test_a_range_taken_from_another_record_reads_back_as_scan_by_scan() -> None:
    prefix, cycle = _flip_flop()
    _stepped, repeated, _planned, last = _recorded_both_ways(prefix, cycle, 40, [])
    window: Scan = {0: ({"a": True, "b": 1}, frozenset())}
    settled: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(20, 31):
        _record(settled, scan_id, window)
    repeated.replace_range(settled, 20, 30)
    # The settled scans replace every column's, so rung 1's columns hold nothing there.
    scans = [*prefix, *cycle * 40]
    scans[19:30] = [window] * 11
    stepped: RungFiringTimelines = RungFiringTimelines()
    for scan_id, scan in enumerate(scans, start=1):
        _record(stepped, scan_id, scan)
    # A column that mixes 0 and False may read back either one.
    _assert_true_to(stepped, stepped, repeated, last)


@pytest.mark.parametrize("seed", range(60))
def test_random_cycles_read_back_as_scan_by_scan(seed: int) -> None:
    rng = random.Random(seed)
    prefix = [_random_scan(rng) for _ in range(rng.randrange(0, 5))]
    cycle = [_random_scan(rng) for _ in range(rng.randrange(1, 5))]
    suffix = [_random_scan(rng) for _ in range(rng.randrange(0, 5))]
    cycles = rng.randrange(3, 25)
    stepped, repeated, _planned, last = _recorded_both_ways(prefix, cycle, cycles, suffix)
    _assert_same(stepped, repeated, last)
    cut = rng.randrange(1, last + 1)
    _assert_same(stepped.snapshot(up_to=cut), repeated.snapshot(up_to=cut), last)


def test_most_random_cycles_go_in_as_a_repeat() -> None:
    planned = 0
    for seed in range(60):
        rng = random.Random(seed)
        prefix = [_random_scan(rng) for _ in range(rng.randrange(0, 5))]
        cycle = [_random_scan(rng) for _ in range(rng.randrange(1, 5))]
        suffix = [_random_scan(rng) for _ in range(rng.randrange(0, 5))]
        cycles = rng.randrange(3, 25)
        planned += _recorded_both_ways(prefix, cycle, cycles, suffix)[2]
    assert planned >= 30


def _stepping(rng: random.Random) -> tuple[list[Scan], Callable[[int], list[Scan]]]:
    """A random cycle whose whole numbers move by a fixed step each cycle, column by column."""
    steps = {(r, t): rng.choice((0, 1, -1, 3)) for r in RUNGS for t in TAGS}
    length = rng.randrange(1, 5)
    shape = []
    for _ in range(length):
        scan = {}
        for rung in RUNGS:
            if rng.random() < 0.6:
                writes: dict[str, Any] = {}
                for tag in TAGS:
                    if rng.random() < 0.8:
                        stepping = steps[(rung, tag)] != 0
                        writes[tag] = rng.randrange(0, 6) if stepping else rng.choice(POOL)
                scan[rung] = (writes, frozenset(t for t in writes if rng.random() < 0.2))
        shape.append(scan)

    def cycle_at(index: int) -> list[Scan]:
        return [
            {
                rung: (
                    {
                        tag: value + steps[(rung, tag)] * index if steps[(rung, tag)] else value
                        for tag, value in writes.items()
                    },
                    varied,
                )
                for rung, (writes, varied) in scan.items()
            }
            for scan in shape
        ]

    prefix = [_random_scan(rng) for _ in range(rng.randrange(0, 5))]
    return prefix, cycle_at


@pytest.mark.parametrize("seed", range(60))
def test_random_stepping_cycles_read_back_as_scan_by_scan(seed: int) -> None:
    rng = random.Random(1000 + seed)
    prefix, cycle_at = _stepping(rng)
    suffix = [_random_scan(rng) for _ in range(rng.randrange(0, 5))]
    cycles = rng.randrange(3, 25)
    stepped, repeated, _planned, last = _recorded_both_ways(prefix, cycle_at, cycles, suffix)
    _assert_same(stepped, repeated, last)
    cut = rng.randrange(1, last + 1)
    _assert_same(stepped.snapshot(up_to=cut), repeated.snapshot(up_to=cut), last)
    early, later = stepped.snapshot(), repeated.snapshot()
    early.trim_before(cut)
    later.trim_before(cut)
    _assert_same(early, later, last)


def test_most_random_stepping_cycles_go_in_with_steps() -> None:
    with_steps = 0
    for seed in range(60):
        rng = random.Random(1000 + seed)
        prefix, cycle_at = _stepping(rng)
        _stepped, repeated, _planned, _last = _recorded_both_ways(prefix, cycle_at, 20, [])
        with_steps += any(
            type(element) is Repeat and element.steps
            for ranges in repeated._value_timelines.values()
            for element in ranges
        )
    assert with_steps >= 20


def _counting_up(index: int) -> list[Scan]:
    """Rung 0 writes a count that goes up one a cycle, rung 1 fires on the other scan."""
    return [{0: ({"a": index, "b": True}, frozenset())}, {1: ({"a": 7}, frozenset())}]


def _kept(cycle: Callable[[int], list[Scan]], cycles: int) -> RungFiringTimelines:
    """The cycles stepped, with every value kept."""
    truth: RungFiringTimelines = _Uncapped()
    scans = [scan for index in range(cycles) for scan in cycle(index)]
    for scan_id, scan in enumerate(scans, start=1):
        _record(truth, scan_id, scan)
    return truth


def test_a_count_with_steps_goes_in_as_one_repeat_past_its_hundredth_value() -> None:
    stepped, repeated, planned, last = _recorded_both_ways([], _counting_up, 300, [])
    assert planned
    assert (0, "a") in stepped._unknown_keys
    assert (0, "a") not in repeated._unknown_keys
    assert repeated.run_count() < 20
    _assert_true_to(stepped, _kept(_counting_up, 300), repeated, last)


def test_appends_after_a_cut_inside_a_repeat_read_back_as_scan_by_scan() -> None:
    # The cut leaves the repeat second to last, and an append reads the last two ranges.
    cycle: list[Scan] = [{0: ({"a": value}, frozenset())} for value in ("p", "q", "r")]
    stepped, repeated, planned, _last = _recorded_both_ways([], cycle, 20, [])
    assert planned
    early, later = stepped.snapshot(up_to=31), repeated.snapshot(up_to=31)
    for scan_id, value in ((32, "r"), (33, "p")):
        _record(early, scan_id, {0: ({"a": value}, frozenset())})
        _record(later, scan_id, {0: ({"a": value}, frozenset())})
    _assert_same(early, later, 33)


def test_a_plan_left_unapplied_leaves_the_count_as_stepping_does() -> None:
    stepped: RungFiringTimelines = RungFiringTimelines()
    planned: RungFiringTimelines = RungFiringTimelines()
    scan_id = 1
    # Three cycles of two scans, rung 0 writing a=1 on the first scan of each.
    for _ in range(3):
        for timelines in (stepped, planned):
            timelines.append(0, scan_id, {"a": 1})
        scan_id += 2
    assert planned.plan_repeat(scan_id - 1, 2) is not None
    for value in range(2, 140):
        for timelines in (stepped, planned):
            timelines.append(0, scan_id, {"a": value})
        scan_id += 2
    assert (0, "a") in stepped._unknown_keys
    _assert_same(stepped, planned, scan_id)


@pytest.mark.parametrize("seed", range(40))
def test_random_plans_left_unapplied_leave_the_column_as_stepping_does(seed: int) -> None:
    rng = random.Random(2000 + seed)
    prefix, cycle_at = _stepping(rng)
    period = len(cycle_at(0))
    scans = [*prefix, *(scan for r in range(3) for scan in cycle_at(r))]
    scans += [_random_scan(rng) for _ in range(rng.randrange(0, 40))]
    stepped: RungFiringTimelines = RungFiringTimelines()
    planned: RungFiringTimelines = RungFiringTimelines()
    warm = len(prefix) + 3 * period
    for scan_id, scan in enumerate(scans, start=1):
        _record(stepped, scan_id, scan)
        _record(planned, scan_id, scan)
        if scan_id == warm:
            planned.plan_repeat(warm, period)
    _assert_same(stepped, planned, len(scans))


def _ramp_then_rest(index: int) -> list[Scan]:
    """A count of three scans that stepping folds into one range, then a scan with no write."""
    return [
        *({0: ({"a": 10 * index + offset}, frozenset())} for offset in range(3)),
        {0: ({}, frozenset())},
    ]


def test_a_count_the_column_folds_away_keeps_the_values_written() -> None:
    stepped, repeated, planned, last = _recorded_both_ways([], _ramp_then_rest, 120, [])
    assert planned
    assert (0, "a") in stepped._unknown_keys
    _assert_true_to(stepped, _kept(_ramp_then_rest, 120), repeated, last)


def test_a_settle_keeps_counting_as_stepping_counts() -> None:
    # The count folds into a range of whole numbers, and stepping still counts its first values.
    scans = [w for index in range(120) for w in _ramp_rows(index)]
    stepped: RungFiringTimelines = RungFiringTimelines()
    for scan_id, writes in enumerate(scans, start=1):
        stepped.append(0, scan_id, writes)
    ours: RungFiringTimelines = RungFiringTimelines()
    settled: RungFiringTimelines = RungFiringTimelines()
    for scan_id, writes in enumerate(scans[:40], start=1):
        ours.append(0, scan_id, writes)
        settled.append(0, scan_id, writes)
    ours.replace_range(settled, 1, 40)
    for scan_id, writes in enumerate(scans[40:], start=41):
        ours.append(0, scan_id, writes)
    _assert_same(stepped, ours, len(scans))
    assert not ours.value_is_known(0, "a")


def _ramp_rows(index: int) -> list[dict[str, Any]]:
    return [{"a": 10 * index}, {"a": 10 * index + 1}, {"a": 10 * index + 2}, {}]


def _typed_scan(rng: random.Random, scan_id: int, kind: str) -> Scan:
    """A scan whose tags each keep one type, as a tag does.

    ``True == 1`` in Python, so a column of both may read back either.
    """
    scan: Scan = {}
    for rung in RUNGS:
        if rng.random() < 0.6:
            writes: dict[str, Any] = {}
            if rng.random() < 0.7:
                if kind == "many":
                    writes["a"] = rng.randrange(150)
                elif kind == "ramp":
                    writes["a"] = scan_id // rng.choice((1, 1, 3))
                else:
                    writes["a"] = rng.choice((0, 1, 2, 7))
            if rng.random() < 0.7:
                writes["b"] = rng.choice((True, False))
            varied = frozenset(tag for tag in writes if rng.random() < 0.2)
            scan[rung] = (writes, varied)
    return scan


@pytest.mark.parametrize("seed", range(200))
def test_a_settle_leaves_the_records_stepping_leaves(seed: int) -> None:
    rng = random.Random(seed)
    kind = rng.choice(("pool", "many", "ramp"))
    count = rng.randrange(10, 500)
    scans = [_typed_scan(rng, scan_id, kind) for scan_id in range(1, count + 1)]
    first = rng.randrange(1, count + 1)
    last = rng.randrange(first, count + 1)
    # The replay steps from a checkpoint at or before *first*, with records of its own.
    anchor = rng.randrange(1, first + 1)
    stepped: RungFiringTimelines = RungFiringTimelines()
    truth: RungFiringTimelines = _Uncapped()
    ours: RungFiringTimelines = RungFiringTimelines()
    settled: RungFiringTimelines = RungFiringTimelines()
    for scan_id, scan in enumerate(scans, start=1):
        _record(stepped, scan_id, scan)
        _record(truth, scan_id, scan)
        if not first <= scan_id <= last:
            _record(ours, scan_id, scan)
        if anchor <= scan_id <= last:
            _record(settled, scan_id, scan)
    ours.replace_range(settled, first, last)
    _assert_true_to(stepped, truth, ours, count)


def test_a_column_gone_unknown_with_a_known_tail_takes_no_plan() -> None:
    # Stepping writes Unknown into the column from now on, so a plan may not lengthen 5.
    timelines: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, 31):
        timelines.append(0, scan_id, {"a": 5})
    timelines._unknown_keys.add((0, "a"))
    assert timelines.plan_repeat(30, 2) is None


def _cycled(first_scan: int, cycles: int) -> tuple[RungFiringTimelines, int]:
    """Three cycles of period 3 appended, and *cycles* more as one repeat.  Returns the next scan."""
    record: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(first_scan, first_scan + 9):
        writes = ({"a": "p"}, {"a": "q"}, {})[(scan_id - first_scan) % 3]
        record.append(0, scan_id, writes)
    plan = record.plan_repeat(first_scan + 8, 3)
    assert plan is not None
    plan.apply(cycles)
    return record, first_scan + 9 + 3 * cycles


def _settle_small(record: RungFiringTimelines, first: int, last: int) -> None:
    """Settle *first*..*last*, and check it held no more than a few megabytes."""
    settled: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(first, last + 1):
        settled.append(0, scan_id, {"a": scan_id * 7})
    tracemalloc.start()
    try:
        record.replace_range(settled, first, last)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4_000_000


def test_a_settle_before_a_long_repeat_keeps_it_one_entry() -> None:
    record, _end = _cycled(14, 300_000)
    _settle_small(record, 1, 10)
    assert record.run_count() < 40


def test_a_settle_after_a_long_repeat_keeps_it_one_entry() -> None:
    record, end = _cycled(1, 300_000)
    _settle_small(record, end, end + 9)
    assert record.run_count() < 40
    assert record.value_at(0, "a", end + 9) == (end + 9) * 7


_SIX = ({"a": 1}, {}, {"a": 2}, {"a": 3}, {"a": 4}, {"a": 5})


def _many_then_six(scan_id: int) -> dict[str, Any]:
    """97 values, then a cycle of six scans."""
    if scan_id < 98:
        return {"a": (scan_id * scan_id) % 1009 + 1000}
    return _SIX[(scan_id - 98) % 6]


def test_a_settle_that_brings_a_column_to_its_hundredth_value_keeps_a_later_repeat() -> None:
    # Ours missed scans 40..60, so it counted fewer values and a plan took the cycles.
    # Stepping went unknown in the first cycle.  The settle leaves the repeat as it was.
    ours: RungFiringTimelines = RungFiringTimelines()
    scan_id = 1
    while scan_id < 98 + 6 * 4:
        if not 40 <= scan_id <= 60:
            ours.append(0, scan_id, _many_then_six(scan_id))
        scan_id += 1
    plan = ours.plan_repeat(scan_id - 1, 6)
    assert plan is not None
    plan.apply(100_000)
    scan_id += 6 * 100_000
    for _ in range(3):
        ours.append(0, scan_id, _many_then_six(scan_id))
        scan_id += 1
    before = ours.run_count()
    settled: RungFiringTimelines = RungFiringTimelines()
    for settled_scan in range(1, 61):
        settled.append(0, settled_scan, _many_then_six(settled_scan))
    tracemalloc.start()
    try:
        ours.replace_range(settled, 40, 60)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert peak < 4_000_000
    assert ours.run_count() <= before + 25
    assert ours.value_at(0, "a", scan_id - 1) == _many_then_six(scan_id - 1)["a"]
    assert ours.value_at(0, "a", 50) == _many_then_six(50)["a"]


def test_a_column_the_loop_could_not_speak_for_keeps_its_later_values() -> None:
    # The loop wrote Unknown at scan 3.  A value written after that is still the value written,
    # even past the scan where stepping stops keeping values.
    record: RungFiringTimelines = RungFiringTimelines()
    record.append(0, 1, {"n": 5, "flag": True})
    record.append(0, 2, {"n": 9, "flag": False})
    record.append(0, 3, {"n": _UNKNOWN_VALUE, "flag": _UNKNOWN_VALUE})
    record.append(0, 4, {"n": 12, "flag": True})
    assert record.value_at(0, "n", 4) == 12
    assert record.value_at(0, "flag", 4) is True


def _squares(record: RungFiringTimelines, first: int, last: int) -> None:
    """A new value nearly every scan, with no run that steps by one amount."""
    for scan_id in range(first, last + 1):
        record.append(0, scan_id, {"a": (scan_id * scan_id) % 1009})


def test_scans_after_a_gap_not_yet_settled_keep_the_value_written() -> None:
    # Scans after a gap not yet settled count values from the gap's end.  So they may keep
    # values past the scan where stepping stops keeping them, and each is the value written.
    stepped: RungFiringTimelines = RungFiringTimelines()
    _squares(stepped, 1, 400)
    live: RungFiringTimelines = RungFiringTimelines()
    _squares(live, 1, 40)
    _squares(live, 161, 400)
    assert stepped.value_at(0, "a", 200) is _UNKNOWN_VALUE
    assert live.value_at(0, "a", 200) == (200 * 200) % 1009 == 649


def _square_or_ramp(scan_id: int) -> int:
    """97 values, then a ramp that stepping keeps as one range, so it never counts 100."""
    return (scan_id * scan_id) % 1009 if scan_id < 98 else 5000 + scan_id


def _squares_then_a_ramp(record: RungFiringTimelines, scans: Any) -> None:
    for scan_id in scans:
        record.append(0, scan_id, {"a": _square_or_ramp(scan_id)})


def test_a_gap_settled_back_half_first_knows_only_values_written() -> None:
    # Settling the back half first counts values from the gap's end, so the column may go
    # unknown early.  Whatever it still knows is the value written.
    live: RungFiringTimelines = RungFiringTimelines()
    _squares_then_a_ramp(live, [*range(1, 150), *range(251, 301)])
    for first, last in ((201, 250), (150, 200)):
        settled: RungFiringTimelines = RungFiringTimelines()
        _squares_then_a_ramp(settled, range(first, last + 1))
        live.replace_range(settled, first, last)
    known = 0
    for scan_id in range(1, 301):
        value = live.value_at(0, "a", scan_id)
        if value is not _UNKNOWN_VALUE:
            assert value == _square_or_ramp(scan_id), scan_id
            known += 1
    assert known >= 200
