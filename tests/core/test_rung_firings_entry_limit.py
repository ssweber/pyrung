"""A column turns Unknown at its last entry, on the same scan however its scans went in.

Stepping keeps ``_ENTRY_LIMIT`` ranges a column, to bound its memory.  A jump, a bulk record
and a settle must each turn the column Unknown on the scan stepping does.  So how() ranks the
same candidate scans whether the jit ran or not.
"""

from __future__ import annotations

import random
from collections.abc import Callable
from typing import Any

import pytest

from pyrung.core import PLC, Bool, Int, Program, Rung, coast_run, copy, math, out, rung_firings
from pyrung.core.coast_run import coast_until
from pyrung.core.rung_firings import _ENTRY_LIMIT, _UNKNOWN_VALUE, RungFiringTimelines
from tests.fold_check import compare_records, seed_records

KEY = (0, "a")
ONE = frozenset((0,))


def _flasher(scan_id: int) -> bool:
    """On for three scans, off for three."""
    return (scan_id - 1) // 3 % 2 == 0


def _held(scan_id: int) -> int:
    """A count that holds each value three scans."""
    return (scan_id - 1) // 3


# Each shape adds one range every three scans.  So the column holds its last entry on the
# first scan of the thousandth range.
SHAPES = [pytest.param(_flasher, 6, id="flasher"), pytest.param(_held, 3, id="held count")]
CROSSING = 3 * (_ENTRY_LIMIT - 1) + 1
LAST = CROSSING + 300


def _stepped(writes: Callable[[int], Any], last: int = LAST) -> RungFiringTimelines:
    stepped: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, last + 1):
        stepped.append(0, scan_id, {"a": writes(scan_id)})
    return stepped


def _assert_same(ours: RungFiringTimelines, stepped: RungFiringTimelines, last: int) -> None:
    for scan_id in range(last + 2):
        mine, theirs = ours.value_at(0, "a", scan_id), stepped.value_at(0, "a", scan_id)
        assert mine is theirs or (_UNKNOWN_VALUE not in (mine, theirs) and mine == theirs), scan_id
    assert ours._unknown_keys == stepped._unknown_keys
    assert ours._entries == stepped._entries
    for before in (last // 2, CROSSING, CROSSING + 1, CROSSING + 7, last + 1):
        assert ours.tag_transition_candidate_scans_before(
            ONE, "a", before
        ) == stepped.tag_transition_candidate_scans_before(ONE, "a", before), before


@pytest.mark.parametrize(("writes", "period"), SHAPES)
def test_stepping_turns_a_column_unknown_after_its_last_entry(writes, period) -> None:
    stepped = _stepped(writes)
    assert stepped._unknown_keys == {KEY: CROSSING}
    assert stepped.value_at(0, "a", CROSSING) == writes(CROSSING)
    assert stepped.value_at(0, "a", CROSSING + 1) is _UNKNOWN_VALUE
    assert len(stepped._value_timelines[KEY]) < _ENTRY_LIMIT + 2 * (LAST - CROSSING) // period


@pytest.mark.parametrize(("writes", "period"), SHAPES)
def test_a_jump_turns_the_column_unknown_on_steppings_scan(writes, period) -> None:
    # Each plan goes as far as it may, as the loop's plans do.  The next cycle goes in scan by scan.
    ours: RungFiringTimelines = RungFiringTimelines()
    scan_id, jumped = 1, 0
    while scan_id <= LAST:
        ours.append(0, scan_id, {"a": writes(scan_id)})
        if scan_id >= 3 * period and scan_id % period == 0:
            plan = ours.plan_repeat(scan_id, period)
            cycles = 0 if plan is None else min(plan.limit, (LAST - scan_id) // period)
            if plan is not None and cycles > 0:
                plan.apply(cycles)
                scan_id += cycles * period
                jumped += cycles
        scan_id += 1
    assert jumped > (LAST // period) * 0.9
    _assert_same(ours, _stepped(writes), LAST)


@pytest.mark.parametrize(("writes", "period"), SHAPES)
@pytest.mark.parametrize("whole", [True, False], ids=["a rung at once", "tag by tag"])
def test_a_bulk_record_turns_the_column_unknown_on_steppings_scan(writes, period, whole) -> None:
    # Each run of one value goes in as one call, as a long run records a held value.
    ours: RungFiringTimelines = RungFiringTimelines()
    first = 1
    while first <= LAST:
        last = first
        while last < LAST and writes(last + 1) == writes(first):
            last += 1
        if whole:
            ours.append_repeated(0, first, last - first + 1, {"a": writes(first)})
        else:
            ours.append_fired_run(0, first, last)
            ours.append_tag_run(0, "a", first, last, writes(first))
        first = last + 1
    _assert_same(ours, _stepped(writes), LAST)


@pytest.mark.parametrize(("writes", "period"), SHAPES)
def test_a_settle_turns_the_column_unknown_on_steppings_scan(writes, period) -> None:
    # The jit left scans near the crossing unrecorded.  A replay steps them again from a
    # checkpoint at or before the gap, with records of its own.
    stepped = _stepped(writes)
    for first in range(CROSSING - 10, CROSSING + 4):
        for last in range(first, first + 8):
            for anchor in (first - 1, first - 2, first - 5, 0):
                ours: RungFiringTimelines = RungFiringTimelines()
                for scan_id in range(1, first):
                    ours.append(0, scan_id, {"a": writes(scan_id)})
                replay = ours.carried_to(anchor)
                for scan_id in range(anchor + 1, last + 1):
                    replay.append(0, scan_id, {"a": writes(scan_id)})
                ours.replace_range(replay, first, last)
                for scan_id in range(last + 1, LAST + 1):
                    ours.append(0, scan_id, {"a": writes(scan_id)})
                _assert_same(ours, stepped, LAST)


@pytest.mark.parametrize(("writes", "period"), SHAPES)
def test_a_settle_after_a_jump_past_the_crossing_turns_it_unknown_there(writes, period) -> None:
    # Ours missed sixty scans, so its plan ran past stepping's crossing.  The settle turns
    # the column Unknown from that scan, and its repeat stays one entry.
    gap = (CROSSING - 100, CROSSING - 41)
    ours: RungFiringTimelines = RungFiringTimelines()
    scan_id = 1
    while scan_id <= LAST:
        if gap[0] <= scan_id <= gap[1]:
            scan_id += 1
            continue
        ours.append(0, scan_id, {"a": writes(scan_id)})
        if scan_id > gap[1] + 3 * period and (scan_id - gap[1]) % period == 0:
            plan = ours.plan_repeat(scan_id, period)
            cycles = 0 if plan is None else min(plan.limit, (LAST - scan_id) // period)
            if plan is not None and cycles > 0:
                plan.apply(cycles)
                scan_id += cycles * period
        scan_id += 1
    assert KEY not in ours._unknown_keys or ours._unknown_keys[KEY] > CROSSING
    replay = ours.carried_to(gap[0] - 6)
    for replayed in range(gap[0] - 5, gap[1] + 1):
        replay.append(0, replayed, {"a": writes(replayed)})
    ours.replace_range(replay, *gap)
    _assert_same(ours, _stepped(writes), LAST)
    assert ours.run_count() < _ENTRY_LIMIT + 50


def test_a_column_turned_unknown_takes_a_plan_once_it_settles() -> None:
    # Stepping keeps a value written every other scan as a flip between Unknown and no write.
    ours: RungFiringTimelines = RungFiringTimelines()
    scan_id = 1
    for value in range(_ENTRY_LIMIT + 10):
        ours.append(0, scan_id, {"a": value})
        ours.append(0, scan_id + 1, {})
        scan_id += 2
    assert KEY in ours._unknown_keys
    plan = ours.plan_repeat(scan_id - 1, 2)
    assert plan is not None
    plan.apply(1_000_000)
    assert ours.run_count() < _ENTRY_LIMIT + 20


# Shapes the last review found wrong.  Each gives the writes up to the settle, the gap, the
# checkpoint the replay starts from, and the writes after the settle.
_Shape = tuple[list[Any], int, int, int, list[Any]]


def _wrong() -> _Shape:
    """A flip-flop window after a held value.  A settle once stored a value never written."""
    return [5] * 10 + [5 if k % 2 == 0 else 7 for k in range(11)] + [9] * 9, 11, 21, 10, []


def _held_pairs(base: int, count: int) -> list[Any]:
    return [base + k for k in range(count) for _ in (0, 1)]


def _unknown_before_the_gap() -> _Shape:
    """The loop wrote Unknown just before the gap, and the column reaches the limit after it."""
    head = _held_pairs(1000, 60)
    writes = [*head, _UNKNOWN_VALUE, *_held_pairs(2000, 10), *_held_pairs(3000, 40)]
    return writes, len(head) + 2, len(head) + 21, len(head) - 10, []


def _limit_before_the_gap() -> _Shape:
    """The last entry lands on the scan before the gap, and the gap holds that value on."""
    head = [*_held_pairs(1000, 99), 5000]
    return [*head, *[5000] * 20, 6000, 6001], len(head) + 1, len(head) + 10, len(head) - 10, []


def _short_prefix(anchor_at_gap: bool) -> _Shape:
    """A merge leaves fewer ranges than scans, then the gap brings ninety-five new values."""
    head = [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 100, 101, 102, 103, 104]
    window = _held_pairs(2000, 95)
    first = len(head) + 1
    last = first + len(window) - 1
    return [*head, *window, 7, 7], first, last, first - 1 if anchor_at_gap else 0, []


def _ramp_across_the_gaps_end() -> _Shape:
    """A count runs across the end of the gap, and later values decide the limit."""
    return [*_held_pairs(1000, 20), *range(200)], 90, 139, 85, _held_pairs(3000, 90)


def _replay_reaches_it_first(goes_on: bool) -> _Shape:
    """The replay starts on a count's last two scans, which stepping merged into one range."""
    if goes_on:
        writes = [*range(200), *_held_pairs(1000, 99)]
        return writes, 350, len(writes), 198, _held_pairs(1099, 11)
    writes = [*range(200), *_held_pairs(1000, 110)]
    return writes, 350, len(writes), 198, []


@pytest.mark.parametrize(
    "shape",
    [
        _wrong,
        _unknown_before_the_gap,
        _limit_before_the_gap,
        lambda: _short_prefix(False),
        lambda: _short_prefix(True),
        _ramp_across_the_gaps_end,
        lambda: _replay_reaches_it_first(False),
        lambda: _replay_reaches_it_first(True),
    ],
    ids=["wrong", "P1", "P2", "P3", "P3 late anchor", "P5", "P6", "P6 goes on"],
)
def test_the_shapes_the_last_review_found_settle_as_stepping_does(
    shape: Callable[[], _Shape], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rung_firings, "_ENTRY_LIMIT", 100)
    writes, first, last, anchor, extra = shape()
    writes = [*writes, *extra]

    def put(timelines: RungFiringTimelines, scan_id: int) -> None:
        timelines.append(0, scan_id, {"a": writes[scan_id - 1]})

    stepped: RungFiringTimelines = RungFiringTimelines()
    ours: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, anchor + 1):
        put(ours, scan_id)
    replay = ours.carried_to(anchor)
    count = len(writes)
    for scan_id in range(1, count + 1):
        put(stepped, scan_id)
        if scan_id > anchor and not first <= scan_id <= last:
            put(ours, scan_id)
        if anchor < scan_id <= last:
            put(replay, scan_id)
        if scan_id == count - len(extra):
            ours.replace_range(replay, first, last)
    for scan_id in range(count + 2):
        mine, theirs = ours.value_at(0, "a", scan_id), stepped.value_at(0, "a", scan_id)
        assert mine is theirs or mine == theirs, scan_id
    assert ours._unknown_keys == stepped._unknown_keys
    assert ours._entries == stepped._entries
    for before in range(1, count + 2, 7):
        assert ours.tag_transition_candidate_scans_before(
            ONE, "a", before
        ) == stepped.tag_transition_candidate_scans_before(ONE, "a", before), before


def test_a_trim_from_the_front_keeps_the_count() -> None:
    # Stepping trims at each checkpoint.  A long run trims only on the scan it ends on.  Both
    # must turn the column Unknown on the same scan.
    trimmed: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, LAST + 1):
        trimmed.append(0, scan_id, {"a": _held(scan_id)})
        if scan_id % 500 == 0:
            trimmed.trim_before(scan_id - 100)
    stepped = _stepped(_held)
    assert trimmed._unknown_keys == stepped._unknown_keys == {KEY: CROSSING}
    cut = stepped.snapshot(up_to=CROSSING - 5)
    assert cut._unknown_keys == {} and cut._entries == {KEY: _ENTRY_LIMIT - 2}
    later = trimmed.snapshot()
    later.trim_before(LAST - 10)
    assert later._unknown_keys == {KEY: CROSSING}


# Each seed writes random scans and leaves gaps as the jit does.  It settles the gaps in any
# order.  These seeds reach the limit while a gap stepping joins to its edges is unsettled.
_EDGE_JOINS = frozenset({9, 14, 26, 157})
POOL = (0, 1, 2, 7)


def _random_scans(rng: random.Random, count: int) -> list[dict[int, dict[str, Any]]]:
    kind = rng.choice(("pool", "many", "ramp"))
    scans = []
    for scan_id in range(1, count + 1):
        scan = {}
        for rung in (0, 1, 2):
            if rng.random() < 0.6:
                writes: dict[str, Any] = {}
                if rng.random() < 0.7:
                    writes["a"] = (
                        rng.randrange(150)
                        if kind == "many"
                        else scan_id // rng.choice((1, 1, 3))
                        if kind == "ramp"
                        else rng.choice(POOL)
                    )
                if rng.random() < 0.7:
                    writes["b"] = rng.choice((True, False))
                scan[rung] = writes
        scans.append(scan)
    return scans


def _gap_fuzz(seed: int) -> tuple[RungFiringTimelines, RungFiringTimelines, int]:
    """Ours with its gaps settled, stepping, and the last scan."""
    rng = random.Random(seed)
    count = rng.randrange(20, 400)
    scans = _random_scans(rng, count)
    gaps: list[tuple[int, int]] = []
    for _ in range(rng.randrange(1, 4)):
        first = rng.randrange(1, count + 1)
        last = min(count, first + rng.randrange(0, 60))
        if all(last < start - 1 or first > end + 1 for start, end in gaps):
            gaps.append((first, last))
    stepped: RungFiringTimelines = RungFiringTimelines()
    ours: RungFiringTimelines = RungFiringTimelines()
    for scan_id, scan in enumerate(scans, start=1):
        for rung, writes in scan.items():
            stepped.append(rung, scan_id, writes)
            if not any(start <= scan_id <= end for start, end in gaps):
                ours.append(rung, scan_id, writes)
    rng.shuffle(gaps)
    for first, last in gaps:
        anchor = rng.randrange(0, first)
        replay = ours.carried_to(anchor)
        for scan_id in range(anchor + 1, last + 1):
            for rung, writes in scans[scan_id - 1].items():
                replay.append(rung, scan_id, writes)
        ours.replace_range(replay, first, last)
    return ours, stepped, count


def _assert_alike(seed: int) -> None:
    ours, stepped, count = _gap_fuzz(seed)
    for rung in (0, 1, 2):
        one = frozenset((rung,))
        for tag in ("a", "b"):
            for scan_id in range(count + 2):
                mine, theirs = (
                    ours.value_at(rung, tag, scan_id),
                    stepped.value_at(rung, tag, scan_id),
                )
                where = (rung, tag, scan_id)
                assert mine is theirs or (
                    _UNKNOWN_VALUE not in (mine, theirs) and mine == theirs
                ), where
            for before in (count // 3, count // 2, count + 1):
                assert ours.tag_transition_candidate_scans_before(
                    one, tag, before
                ) == stepped.tag_transition_candidate_scans_before(one, tag, before)
    assert ours._unknown_keys == stepped._unknown_keys
    assert ours._entries == stepped._entries


@pytest.mark.parametrize("seed", [seed for seed in range(400) if seed not in _EDGE_JOINS])
def test_gaps_settled_in_any_order_leave_the_records_stepping_leaves(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rung_firings, "_ENTRY_LIMIT", 40)
    _assert_alike(seed)


@pytest.mark.xfail(strict=True, reason="a column reaches its limit before a gap settles")
@pytest.mark.parametrize("seed", sorted(_EDGE_JOINS))
def test_a_column_that_reaches_its_limit_before_a_joined_gap_settles(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Stepping joins the gap to the ranges on both sides.  So the records after a gap count a
    # range or two more than stepping.  A column that reaches its limit before the gap settles
    # turns Unknown early, by up to about 1,000 scans.  The settle cannot take that back.
    monkeypatch.setattr(rung_firings, "_ENTRY_LIMIT", 40)
    _assert_alike(seed)


@pytest.mark.parametrize("seed", sorted(_EDGE_JOINS))
def test_a_column_that_turns_unknown_early_still_knows_only_values_written(
    seed: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rung_firings, "_ENTRY_LIMIT", 40)
    ours, stepped, count = _gap_fuzz(seed)
    for rung in (0, 1, 2):
        for tag in ("a", "b"):
            for scan_id in range(count + 2):
                mine = ours.value_at(rung, tag, scan_id)
                if mine is not _UNKNOWN_VALUE:
                    assert mine == stepped.value_at(rung, tag, scan_id), (rung, tag, scan_id)


def _review_scans(rng: random.Random, count: int) -> list[tuple[str, Any]]:
    """Held values, counts, flip-flops, slow flashers, noise and scans with no write.

    Each event is a write ("w"), a firing with no write ("m"), or no firing ("n").
    """
    events: list[tuple[str, Any]] = []
    pool = rng.choice([3, 10, 40, 300])
    while len(events) < count:
        kind = rng.choice("cccaalltrrnmup")
        length = rng.randint(1, 30)
        if kind == "c":
            part = [("w", rng.randrange(pool))] * length
        elif kind == "a":
            value, step = rng.randrange(pool), rng.choice([1, -1, 2, 3])
            part = [("w", value + step * k) for k in range(length)]
        elif kind == "l":
            on, off = rng.randrange(pool), rng.randrange(pool)
            part = [("w", on if k % 2 == 0 else off) for k in range(length)]
        elif kind == "r":
            part = [("w", rng.randrange(pool)) for _ in range(length)]
        elif kind == "t":
            part = [("w", rng.randrange(pool)), ("m", None)] * (length // 2 + 1)
        elif kind == "p":
            period, base = rng.randint(2, 5), rng.randrange(pool)
            part = [
                ("w", base if k < period // 2 + 1 else base + 100)
                for _ in range(rng.randint(3, 40))
                for k in range(period)
            ]
        elif kind in "nm":
            part = [(kind, None)] * min(length, 4)
        else:
            part = [("w", _UNKNOWN_VALUE)] * min(length, 2) if rng.random() < 0.3 else []
        events.extend(part)
    events = events[:count]
    events[0] = ("w", 0)
    return events


def _review_put(records: RungFiringTimelines, scan_id: int, event: tuple[str, Any]) -> None:
    kind, value = event
    if kind != "n":
        records.append(0, scan_id, {} if kind == "m" else {"a": value})


def _review_jump(
    ours: RungFiringTimelines, events: list, scan_id: int, end: int, since: int
) -> int:
    """Scans a plan records from *scan_id*, once three cycles after the last gap are alike."""
    for period in range(1, 7):
        if scan_id - since < 3 * period or not all(
            events[scan_id - 1 - j]
            == events[scan_id - 1 - j - period]
            == events[scan_id - 1 - j - 2 * period]
            for j in range(period)
        ):
            continue
        cycles = 0
        while scan_id + (cycles + 1) * period <= end and all(
            events[scan_id + cycles * period + j] == events[scan_id - period + j]
            for j in range(period)
        ):
            cycles += 1
        plan = ours.plan_repeat(scan_id, period) if cycles else None
        if plan is not None and min(plan.limit, cycles) >= 1:
            plan.apply(min(plan.limit, cycles))
            return min(plan.limit, cycles) * period
    return 0


def _review_trial(seed: int, mode: str, jumps: str, trims: str) -> tuple[Any, ...]:
    """Ours with its gaps settled, stepping, the events, the last scan and the trim's floor."""
    rng = random.Random(seed)
    count = rng.randint(80, 900)
    events = _review_scans(rng, count)
    gaps: list[tuple[int, int]] = []
    if mode == "tail":
        first = rng.randint(count // 4, count // 2)
        gaps.append((first, min(count, first + rng.randint(20, count // 2))))
    else:
        for _ in range(rng.randrange(1, 4)):
            first = rng.randint(2, count)
            last = min(count, first + rng.randrange(0, 80))
            if all(last < start - 1 or first > end + 1 for start, end in gaps):
                gaps.append((first, last))
    jumped = rng.random() < 0.5 if jumps == "r" else jumps == "1"
    if trims == "r":
        trim_at = rng.choice([None, rng.randint(10, count)])
    else:
        trim_at = rng.randint(10, count) if trims == "1" else None
    floor = trim_at // 2 if trim_at is not None else 0
    gaps = [(max(first, floor + 1), last) for first, last in gaps if max(first, floor + 1) <= last]
    stepped: RungFiringTimelines = RungFiringTimelines()
    ours: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, count + 1):
        _review_put(stepped, scan_id, events[scan_id - 1])
        if scan_id == trim_at:
            stepped.trim_before(floor)
    scan_id, trimmed = 1, trim_at is None
    while scan_id <= count:
        if any(first <= scan_id <= last for first, last in gaps):
            scan_id += 1
            continue
        _review_put(ours, scan_id, events[scan_id - 1])
        if trim_at is not None and not trimmed and scan_id >= trim_at:
            ours.trim_before(floor)
            trimmed = True
        if jumped and rng.random() < 0.3:
            end = min([first for first, _last in gaps if first > scan_id] + [count + 1])
            since = max([last for _first, last in gaps if last < scan_id] + [0])
            scan_id += _review_jump(ours, events, scan_id, end - 1, since)
        scan_id += 1
    if not trimmed:
        ours.trim_before(floor)  # The run ended in a gap, and the trim comes after it.
    order = list(gaps)
    rng.shuffle(order)
    for first, last in order:
        anchor = rng.randrange(floor, first)
        replay = ours.carried_to(anchor)
        for scan_id in range(anchor + 1, last + 1):
            _review_put(replay, scan_id, events[scan_id - 1])
        ours.replace_range(replay, first, last)
    return ours, stepped, count, floor


@pytest.mark.parametrize(
    ("seed", "limit", "mode", "jumps", "trims"),
    [
        pytest.param(44, 1000, "tail", "0", "1", id="44 the trim's floor meets the gap"),
        pytest.param(231, 1000, "tail", "0", "1", id="231 the trim's floor meets the gap"),
        pytest.param(952, 40, "random", "0", "1", id="952 the trim's floor meets the gap"),
        pytest.param(45, 40, "random", "1", "1", id="45 a settle reads the trim's count"),
        pytest.param(202, 40, "random", "r", "r", id="202 a settle reads the trim's count"),
    ],
)
def test_a_trimmed_column_settles_as_stepping_left_it(
    seed: int, limit: int, mode: str, jumps: str, trims: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A settle once took the count a trim left from the scan the column turned Unknown on.
    # Another joined the cut range at the trim's floor to the scans of the gap.
    monkeypatch.setattr(rung_firings, "_ENTRY_LIMIT", limit)
    ours, stepped, count, floor = _review_trial(seed, mode, jumps, trims)
    for scan_id in range(floor + 1, count + 2):
        mine, theirs = ours.value_at(0, "a", scan_id), stepped.value_at(0, "a", scan_id)
        assert mine is theirs or (_UNKNOWN_VALUE not in (mine, theirs) and mine == theirs), scan_id
    assert ours._unknown_keys == stepped._unknown_keys
    assert ours._entries == stepped._entries
    for before in sorted({count + 1, count // 2, count // 3, count - 5}):
        if before > floor + 1:
            assert ours.tag_transition_candidate_scans_before(
                ONE, "a", before
            ) == stepped.tag_transition_candidate_scans_before(ONE, "a", before), before


def test_settles_in_a_row_keep_the_scan_a_column_turned_unknown_on() -> None:
    # A flip-flop with a gap of seven scans before each stepped scan.  The records hold each
    # stepped scan as a range of its own.  So the column turns Unknown before stepping's does.
    # Settling the gaps in scan order must not move that scan earlier still.
    count, gaps = 9000, [(first, first + 6) for first in range(2, 8995, 8)]
    ours: RungFiringTimelines = RungFiringTimelines()
    for scan_id in range(1, count + 1):
        if (scan_id - 2) % 8 == 7 or scan_id == 1:
            ours.append(0, scan_id, {"a": scan_id % 2 == 0})
    crossing = ours._unknown_keys[KEY]
    for first, last in gaps:
        anchor = (first - 1) // 100 * 100
        replay = ours.carried_to(anchor)
        for scan_id in range(anchor + 1, last + 1):
            replay.append(0, scan_id, {"a": scan_id % 2 == 0})
        ours.replace_range(replay, first, last)
    assert ours._unknown_keys == {KEY: crossing}
    assert all(ours.value_at(0, "a", s) == (s % 2 == 0) for s in range(1, crossing))


Phase = Int("EntryPhase")
Light = Bool("EntryLight")
Count = Int("EntryCount")
Shown = Int("EntryShown")


def _flasher_program() -> Program:
    with Program(strict=False) as program:
        with Rung():
            math((Phase + 1) % 6, Phase)
        with Rung(Phase < 3):
            out(Light)
    return program


def _held_count_program() -> Program:
    with Program(strict=False) as program:
        with Rung():
            math((Phase + 1) % 3, Phase)
        with Rung(Phase == 0):
            math(Count + 1, Count)
        with Rung():
            copy(Count, Shown)
    return program


@pytest.mark.parametrize("build", [_flasher_program, _held_count_program])
def test_a_long_run_turns_a_column_unknown_on_steppings_scan(build) -> None:
    plc = PLC(build(), dt=0.01)
    plc.step()
    twin = plc.fork()
    seed_records(twin, plc)
    start = plc.current_state.scan_id
    stats: dict[str, int] = {}
    coast_until(plc, lambda _s: False, budget=4000, predicate_reads=frozenset(), stats=stats)
    while twin.current_state.scan_id < plc.current_state.scan_id:
        twin.step()
    assert stats["stretched_scans"] > 2000
    stepped = twin._rung_firing_timelines._unknown_keys
    assert stepped
    assert plc._rung_firing_timelines._unknown_keys == stepped
    assert coast_run.MAX_RECORD_RUNS > plc._rung_firing_timelines.run_count()
    compare_records("entry limit", plc, twin, start)


Enable = Bool("EntryEnable")
Lamp = Bool("EntryLamp")


def test_runs_with_gaps_settle_to_the_records_stepping_keeps() -> None:
    # The first run keeps Enable off, so Shown holds 0 across the gap after it.  Stepping
    # keeps that as one range.  The later runs count, and each reaches past the limit.
    with Program(strict=False) as program:
        with Rung():
            math((Phase + 1) % 3, Phase)
        with Rung(Phase == 0, Enable):
            math(Count + 1, Count)
        with Rung():
            copy(Count, Shown)
        with Rung(Shown > 5):
            out(Lamp)
    plc = PLC(program, dt=0.01)
    twin = PLC(program, dt=0.01)
    for run in range(5):
        plc.patch({Enable.name: run != 0})
        twin.patch({Enable.name: run != 0})
        plc.run_for(30)
        while twin.current_state.scan_id < plc.current_state.scan_id:
            twin.step()
    plc._settle_compiled_ranges(None, None, at_any_cost=True)
    last = plc.current_state.scan_id
    for name in ("_rung_firing_timelines", "_committed_tag_timelines"):
        ours, stepped = getattr(plc, name), getattr(twin, name)
        assert ours._unknown_keys == stepped._unknown_keys, name
        for rung, tag in ours._value_timelines.keys() | stepped._value_timelines.keys():
            for scan_id in range(1, last + 1):
                mine, theirs = (
                    ours.value_at(rung, tag, scan_id),
                    stepped.value_at(rung, tag, scan_id),
                )
                where = (name, rung, tag, scan_id)
                assert mine is theirs or (
                    _UNKNOWN_VALUE not in (mine, theirs) and mine == theirs
                ), where
