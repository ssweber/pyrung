"""A run recorded as a repeat reads back as the same run recorded scan by scan."""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.core.rung_firings import Repeat, RungFiringTimelines

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


def _reads(timelines: RungFiringTimelines, last: int) -> dict[str, Any]:
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
            rung: sorted((tag, _shown(value)) for tag, value in writes.items())
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
                out[f"value {key}"] = _shown(timelines.value_at(rung, tag, scan_id))
                out[f"varied {key}"] = timelines.varied_on(rung, tag, scan_id)
                last_write = timelines.last_tag_write_before(one, tag, scan_id)
                out[f"last write {key}"] = (
                    None if last_write is None else (last_write[0], _shown(last_write[1]))
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
        added = min(plan.limit, cycles - 3)
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
    # A column goes unknown at its hundredth value, so later scans need the same count.
    assert repeated._unknown_keys == stepped._unknown_keys
    assert _counts(repeated) == _counts(stepped)


def _counts(timelines: RungFiringTimelines) -> dict[Any, list[str]]:
    return {key: sorted(map(_shown, values)) for key, values in timelines._distinct_values.items()}


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
    stepped, repeated, _planned, last = _recorded_both_ways(prefix, cycle, 40, [])
    settled: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(20, 31):
        _record(settled, scan_id, {0: ({"a": True, "b": 1}, frozenset())})
    stepped.replace_range(settled, 20, 30)
    repeated.replace_range(settled, 20, 30)
    _assert_same(stepped, repeated, last)


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


def test_a_count_steps_until_its_column_would_go_unknown() -> None:
    stepped, repeated, planned, last = _recorded_both_ways([], _counting_up, 300, [])
    assert planned
    assert (0, "a") in stepped._unknown_keys
    assert (0, "a") in repeated._unknown_keys
    _assert_same(stepped, repeated, last)


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


def test_a_count_the_column_folds_away_is_counted_as_stepping_counts_it() -> None:
    # Stepping counts the first two values of each ramp, and the third joins their range.
    stepped, repeated, planned, last = _recorded_both_ways([], _ramp_then_rest, 120, [])
    assert planned
    assert (0, "a") in stepped._unknown_keys
    _assert_same(stepped, repeated, last)


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
    ours: RungFiringTimelines = RungFiringTimelines()
    settled: RungFiringTimelines = RungFiringTimelines()
    for scan_id, scan in enumerate(scans, start=1):
        _record(stepped, scan_id, scan)
        if not first <= scan_id <= last:
            _record(ours, scan_id, scan)
        if anchor <= scan_id <= last:
            _record(settled, scan_id, scan)
    ours.replace_range(settled, first, last)
    _assert_same(stepped, ours, count)


def test_a_column_gone_unknown_with_a_known_tail_takes_no_plan() -> None:
    # Stepping writes Unknown into the column from now on, so a plan may not lengthen 5.
    timelines: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, 31):
        timelines.append(0, scan_id, {"a": 5})
    timelines._unknown_keys.add((0, "a"))
    assert timelines.plan_repeat(30, 2) is None
