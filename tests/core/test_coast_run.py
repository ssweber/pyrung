"""A coast lands where stepping lands, whatever the program does between its scans.

Each program moves something in a way a scan's trace might describe wrongly, and each
coast runs next to a stepped twin that must match it after every loop firing.
"""

from __future__ import annotations

import math
import random
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    TagType,
    Timer,
    calc,
    coast_run,
    compiled_run,
    copy,
    latch,
    on_delay,
    out,
    reset,
    rise,
    runner,
    scan_trace,
    system,
)
from pyrung.core.coast_run import _record_columns, _Recording, coast_until, memo_of
from pyrung.core.rung_firings import _UNKNOWN_VALUE, RungFiringTimelines, UnrecordedScansError
from pyrung.core.tag import Dint
from tests.fold_check import compare_records

CASES = {}


def case(f):
    CASES[f.__name__] = f
    return f


A = Bool("AdvA", external=True)
T = Timer.clone("AdvT")
T2 = Timer.clone("AdvT2")
X, Y, Z, Tmp, Idx = (Int(n) for n in "AdvX AdvY AdvZ AdvTmp AdvIdx".split())
BX, BY, Hit = (Bool(n) for n in "AdvBX AdvBY AdvHit".split())
R1 = Real("AdvR1")
D1 = Dint("AdvD1")
Bank = Block("AdvBank", TagType.INT, 1, 8)


@case
def moved_write_overwritten_later():
    # A rung attempts a moving value; a later rung overwrites it. Committed never moves.
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc, X)
    with Rung():
        copy(0, X)


@case
def consume_on_read_temp():
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc, Tmp)
    with Rung():
        calc(Tmp + 1, Y)
    with Rung():
        copy(0, Tmp)


@case
def same_rung_two_values():
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc, X)
        copy(T.Acc + 1, X)


@case
def two_writers_same_moved_value():
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc, X)
    with Rung():
        copy(T.Acc, X)


@case
def timer_saturates():
    with Rung():
        on_delay(T, 32000)
    with Rung(T.Done):
        latch(Hit)


@case
def staircase_mirror():
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc // 1000, X)
    with Rung(X == 3):
        out(BX)


@case
def indirect_moving_pointer():
    with Rung():
        on_delay(T, 30000)
    with Rung():
        calc((T.Acc // 500) % 8 + 1, Idx)
    with Rung():
        copy(7, Bank[Idx])


@case
def bool_copy_of_clock_unguarded():
    with Rung(system.sys.clock_100ms):
        out(BX)
    with Rung(BX):
        calc(X + 1, X)


@case
def edge_count_on_clock():
    with Rung(rise(system.sys.clock_100ms)):
        calc(X + 1, X)


@case
def oneshot_on_done():
    with Rung():
        on_delay(T, 700)
    with Rung(T.Done):
        copy(5, X, oneshot=True)
    with Rung(T.Done):
        reset(Tmp)


@case
def self_calc_every_scan():
    with Rung():
        calc(X + 7, X)
    with Rung(X > 100):
        copy(0, X)


@case
def real_ramp():
    with Rung():
        calc(R1 + 0.5, R1)
    with Rung(R1 > 700.0):
        latch(Hit)


@case
def dint_big_delta():
    with Rung():
        calc(D1 + 100000, D1)
    with Rung(D1 > 100000000):
        latch(Hit)


@case
def rung_with_no_moving_input_but_later_writer_moves():
    with Rung():
        copy(5, Y)
    with Rung():
        on_delay(T, 2000)
    with Rung(T.Acc > 1000):
        copy(9, Y)


@case
def rtc_second_mirror():
    with Rung():
        copy(system.rtc.second, X)
    with Rung(X == 7):
        out(BX)


@case
def same_value_rewrite_by_a_different_rung():
    # The value stays; who writes it changes when a timer crosses.
    with Rung():
        on_delay(T, 900)
    with Rung(T.Done):
        copy(3, Y)
    with Rung(~T.Done):
        copy(3, Y)


@case
def attempted_differs_from_commit_by_later_constant():
    with Rung():
        on_delay(T, 30000)
    with Rung():
        out(BX)
    with Rung(T.Acc > 500):
        out(BX, False) if False else copy(1, Y)


SCANS = 600


def _program(build):
    with Program(strict=False) as program:
        build()
    return program


def _runner(build, dt: float) -> PLC:
    plc = PLC(_program(build), dt=dt)
    plc.patch({A: False})
    plc.step()
    return plc


def _same_tags(ours: dict[str, Any], theirs: dict[str, Any]) -> None:
    for name in sorted(set(ours) | set(theirs)):
        assert name in ours and name in theirs and repr(ours[name]) == repr(theirs[name]), name


@pytest.fixture
def strict(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(scan_trace, "STRICT_SELF_CHECK", True)
    monkeypatch.setattr(compiled_run, "LOOP_OBSERVERS", [])


def _twin_checker(first: PLC):
    twin = first.fork()

    def observe(_plc: PLC, state: Any) -> None:
        while twin.current_state.scan_id < state.scan_id:
            twin.step()
        _same_tags(dict(state.tags), dict(twin.current_state.tags))
        assert state.memory == twin.current_state.memory
        assert state.timestamp == pytest.approx(twin.current_state.timestamp, abs=1e-9)

    return twin, observe


@pytest.mark.parametrize("name", sorted(CASES))
class TestAgainstStepping:
    @pytest.mark.parametrize("dt", [0.01, 0.3])
    def test_runs_out_its_budget_in_the_loop(self, strict, name: str, dt: float) -> None:
        plc = _runner(CASES[name], dt)
        twin, observe = _twin_checker(plc)
        compiled_run.LOOP_OBSERVERS.append(observe)
        stats: dict[str, int] = {}
        coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset(), stats=stats)
        assert plc.current_state.scan_id - 1 == SCANS
        observe(plc, plc.current_state)
        assert stats["logical_scans"] == SCANS
        # A stretch's landing is the one scan both count.
        assert SCANS <= stats["kernel_scans"] + stats["ordinary_folded_scans"] <= SCANS + 1

    def test_leaves_the_records_stepping_leaves(self, strict, name: str) -> None:
        plc = _runner(CASES[name], 0.3)
        twin = plc.fork()
        whole = _runner(CASES[name], 0.3)  # the surveys read a runner's own epoch, not a fork's
        for _ in range(SCANS):
            twin.step()
            whole.step()
        start = plc.current_state.scan_id
        coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
        compare_records("coast", plc, twin, start)
        assert plc.query.cold_rungs() == whole.query.cold_rungs()
        assert plc.query.hot_rungs() == whole.query.hot_rungs()
        tip = plc.current_state.scan_id
        tags = sorted(n for n in plc.current_state.tags if not n.startswith("sys."))
        for tag in tags:
            for scan in (start + 1, start + SCANS // 2, tip):
                assert str(plc.cause(tag, scan)) == str(twin.cause(tag, scan)), (tag, scan)
            assert str(plc.effect(tag, start + 1, max_scans=50)) == str(
                twin.effect(tag, start + 1, max_scans=50)
            ), tag

    def test_watched_tag_stops_on_its_own_scan(self, strict, name: str) -> None:
        # Stop on the first change of everything the program writes, in turn.
        plc = _runner(CASES[name], 0.3)
        watched = sorted(n for n in plc.current_state.tags if not n.startswith("sys."))
        for tag in watched:
            held = plc.current_state.tags.get(tag)
            plain = plc.fork()
            coast_until(
                plain,
                lambda s, t=tag, h=held: repr(s.tags.get(t)) != repr(h),
                budget=SCANS,
                predicate_reads=frozenset((tag,)),
            )
            traced = plc.fork()
            coast_until(
                traced,
                lambda s, t=tag, h=held: repr(s.tags.get(t)) != repr(h),
                budget=SCANS,
            )
            twin = plc.fork()
            for _ in range(SCANS):
                if repr(twin.current_state.tags.get(tag)) != repr(held):
                    break
                twin.step()
            assert plain.current_state.scan_id == twin.current_state.scan_id, tag
            assert traced.current_state.scan_id == twin.current_state.scan_id, tag
            _same_tags(dict(plain.current_state.tags), dict(twin.current_state.tags))
            _same_tags(dict(traced.current_state.tags), dict(twin.current_state.tags))


def _ramp() -> None:
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc // 500, X)
    with Rung(X == 3):
        out(BX)


def _count_while_forced() -> None:
    with Rung(A):
        calc(X + 1, X)


InsideT = Timer.clone("InsideT")


def _set_and_reset_inside_a_wait() -> None:
    with Rung():
        on_delay(InsideT, 32_000)
    with Rung(InsideT.Acc == 10_000):
        latch(Hit)
    with Rung(InsideT.Acc == 20_000):
        reset(Hit)


def _columns(timelines: RungFiringTimelines[str]) -> str:
    # By repr: a NaN in a column is equal to nothing, itself included.
    return repr(sorted(timelines._value_timelines.items()))


class _FakePath:
    def __init__(self, recordings: dict[str, _Recording]) -> None:
        self.capture = SimpleNamespace(recordings=(recordings,))


def _random_recording(rng: random.Random) -> _Recording:
    tags = rng.sample(["A", "B", "C", "D", "E"], rng.randint(0, 5))
    settled: dict[str, Any] = {}
    from_ends: list[tuple[str, int | None, bool]] = []
    varied = frozenset(tag for tag in tags if rng.random() < 0.2)
    for tag in tags:
        kind = rng.random()
        if kind < 0.3:
            from_ends.append((tag, rng.choice([1, -1, 2, 0, None]), tag in varied))
        elif kind < 0.4:
            settled[tag] = _UNKNOWN_VALUE
        else:
            settled[tag] = rng.choice([0, 1, 1.0, True, False, "x", math.nan])
    return _Recording(settled, tuple(from_ends), varied)


class TestRecordedRuns:
    """A stretch's records land as appending every scan one by one would."""

    @pytest.mark.parametrize("seed", range(60))
    def test_runs_equal_appending_each_scan(self, seed: int) -> None:
        rng = random.Random(seed)
        paths = [
            _FakePath(
                {
                    key: _random_recording(rng)
                    for key in ("rung0", "rung1", "rung2")
                    if rng.random() < 0.7
                }
            )
            for _ in range(rng.randint(1, 4))
        ]
        runs: RungFiringTimelines[str] = RungFiringTimelines()
        scans: RungFiringTimelines[str] = RungFiringTimelines()
        current = {"A": 5, "B": 7, "C": 100, "D": 0, "E": 2}
        scan = 1
        batch: list[SimpleNamespace] = []
        for _ in range(rng.randint(1, 80)):
            path = rng.choice(paths)
            count = rng.choice([1, 1, 1, 2, 3, 4, 7, 30])
            if rng.random() < 0.05:
                current[rng.choice("ABCDE")] += rng.randint(-3, 3)
            ends = {}
            for recording in path.capture.recordings[0].values():
                for tag, delta, _varied in recording.from_ends:
                    ends[tag] = current[tag] + (delta or 0) * (count - 1)
            # Deltas from two rungs on one tag disagree, as no path's would: each rung has its own.
            for key, recording in path.capture.recordings[0].items():
                for offset in range(count):
                    writes = dict(recording.settled)
                    for tag, delta, _varied in recording.from_ends:
                        writes[tag] = (
                            ends[tag] if delta is None else ends[tag] - delta * (count - 1 - offset)
                        )
                    scans.append(key, scan + offset, writes, recording.varied)
            batch.append(SimpleNamespace(path=path, first=scan, count=count, ends=ends))
            current.update(ends)
            scan += count
            if rng.random() < 0.1:
                scan += rng.randint(1, 3)
            if rng.random() < 0.1:
                _record_columns(runs, batch, 0, frozenset())
                batch = []
        _record_columns(runs, batch, 0, frozenset())

        assert runs._fired_ranges == scans._fired_ranges
        assert _columns(runs) == _columns(scans)
        assert runs._varied_ranges == scans._varied_ranges
        assert runs._tags_by_rung == scans._tags_by_rung
        assert runs._writers_by_tag == scans._writers_by_tag
        assert runs._unknown_keys == scans._unknown_keys
        assert runs._partly_unknown == scans._partly_unknown

    def test_a_tag_of_many_values_goes_unknown_as_it_would(self) -> None:
        path = _FakePath({"rung": _Recording({}, (("A", None, False),), frozenset())})
        runs: RungFiringTimelines[str] = RungFiringTimelines()
        scans: RungFiringTimelines[str] = RungFiringTimelines()
        segments = []
        for scan in range(1, 400):
            value = scan * scan % 97
            scans.append("rung", scan, {"A": value})
            segments.append(SimpleNamespace(path=path, first=scan, count=1, ends={"A": value}))
        _record_columns(runs, segments, 0, frozenset())
        assert runs._unknown_keys == scans._unknown_keys
        assert runs._value_timelines == scans._value_timelines

    def test_a_column_that_goes_unknown_in_a_head_stays_unknown(self) -> None:
        repeated: RungFiringTimelines[str] = RungFiringTimelines()
        tag_run: RungFiringTimelines[str] = RungFiringTimelines()
        scans: RungFiringTimelines[str] = RungFiringTimelines()
        scan = 1
        for value in range(0, 1200, 2):
            for timelines in (repeated, tag_run, scans):
                timelines.append("rung", scan, {"A": value * value})
            scan += 1
            if scans._unknown_keys:
                break
        else:
            pytest.fail("no column went unknown")
        repeated.append_repeated("rung", scan, 9, {"A": 1})
        tag_run.append_fired_run("rung", scan, scan + 8)
        tag_run.append_tag_run("rung", "A", scan, scan + 8, 1)
        for offset in range(9):
            scans.append("rung", scan + offset, {"A": 1})
        assert repeated._value_timelines == scans._value_timelines
        assert tag_run._value_timelines == scans._value_timelines

    def test_a_cold_tag_costs_a_run_not_a_scan(self) -> None:
        # One rung, its one tag moving and its others still: a million scans in a few calls.
        recording = _Recording({"Still": 3}, (("Acc", 1, False),), frozenset())
        path = _FakePath({"rung": recording})
        calls = {"tag": 0}

        class Counting(RungFiringTimelines[str]):
            __slots__ = ()

            def append_tag_run(self, *args: Any, **kwargs: Any) -> None:
                calls["tag"] += 1
                super().append_tag_run(*args, **kwargs)

        runs = Counting()
        segments = [
            SimpleNamespace(path=path, first=scan, count=1, ends={"Acc": scan})
            for scan in range(1, 5001)
        ]
        _record_columns(runs, segments, 0, frozenset())
        assert calls["tag"] == 2
        assert dict(runs.rung_writes_at("rung", 5000)) == {"Still": 3, "Acc": 5000}


class TestARungThatFiresInsideARange:
    """Set and reset where no scan outside the range would see it."""

    def _stepped(self, scans: int) -> PLC:
        plc = _runner(_set_and_reset_inside_a_wait, 0.1)
        for _ in range(scans):
            plc.step()
        return plc

    def _check(self, plc: PLC, stepped: PLC) -> None:
        assert plc.current_state.scan_id == stepped.current_state.scan_id
        assert plc.query.cold_rungs() == stepped.query.cold_rungs()
        assert plc.query.hot_rungs() == stepped.query.hot_rungs()
        tip = plc.current_state.scan_id
        for scan in range(95, 105):
            assert plc.rung_firings(scan) == stepped.rung_firings(scan), scan
        for scan in (101, 150, 201, tip):
            assert str(plc.cause(Hit, scan)) == str(stepped.cause(Hit, scan)), scan
        assert str(plc.cause(Hit)) == str(stepped.cause(Hit))
        assert str(plc.effect(Hit, 101, max_scans=200)) == str(
            stepped.effect(Hit, 101, max_scans=200)
        )

    def test_in_a_coast(self, strict) -> None:
        plc = _runner(_set_and_reset_inside_a_wait, 0.1)
        stats: dict[str, int] = {}
        coast_until(plc, lambda _s: False, budget=600, predicate_reads=frozenset(), stats=stats)
        assert stats["ordinary_folded_scans"] > 500
        self._check(plc, self._stepped(600))

    def test_in_a_compiled_run(self, loop_from_first_scan) -> None:
        plc = _runner(_set_and_reset_inside_a_wait, 0.1)
        plc.run_for(0.1 * 600)
        assert plc._fold_windows
        self._check(plc, self._stepped(600))


def _writes_two_values_a_scan() -> None:
    # A loop's record cannot say which attempt was the rung's last.
    with Rung():
        on_delay(T, 30000)
    with Rung():
        copy(T.Acc, X)
        calc(T.Acc + 1, X)
    with Rung(X > 2):
        out(Hit)


def _writes_too_many_values() -> None:
    # A stepped run's record gives up on a column this various.
    with Rung():
        on_delay(T, 30000)
    with Rung():
        calc(T.Acc * T.Acc % 1009, X)
    with Rung(X > 2):
        out(Hit)


class TestRungFiringsAlwaysHaveTheirValues:
    SCANS = 1000

    def _stepped(self, build) -> PLC:
        plc = _runner(build, 0.01)
        for _ in range(self.SCANS):
            plc.step()
        return plc

    def _coasted(self, build) -> PLC:
        plc = _runner(build, 0.01)
        coast_until(plc, lambda _state: False, budget=self.SCANS)
        return plc

    @pytest.mark.parametrize("build", [_writes_two_values_a_scan, _writes_too_many_values])
    @pytest.mark.parametrize("run", ["_stepped", "_coasted"])
    def test_each_says_what_the_scan_wrote(self, build, run: str) -> None:
        plc = getattr(self, run)(build)
        stepped = self._stepped(build)
        for scan in (1, 2, 150, 500, 777, self.SCANS):
            firings = plc.rung_firings(scan)
            assert firings == stepped.rung_firings(scan), scan
            assert firings[1]["AdvX"] == plc.history.at(scan).tags["AdvX"], scan

    def test_the_records_did_give_up_on_these(self) -> None:
        # Or the test above asks nothing of the replay.
        for plc in (
            self._coasted(_writes_two_values_a_scan),
            self._stepped(_writes_too_many_values),
        ):
            assert plc._recorded_rung_firings(777)[1]["AdvX"] is _UNKNOWN_VALUE

    def test_too_far_to_step_again_is_a_named_error(self, monkeypatch) -> None:
        plc = self._stepped(_writes_too_many_values)
        monkeypatch.setattr(runner, "SETTLE_SCAN_LIMIT", 10)
        anchor = plc._nearest_checkpoint_at_or_before(776)
        assert anchor is not None and 777 - anchor > 10
        with pytest.raises(UnrecordedScansError, match=r"scans 777\.\.777 include"):
            plc.rung_firings(777)


def test_a_long_wait_runs_in_the_loop() -> None:
    plc = _runner(_ramp, 0.01)
    stats: dict[str, int] = {}
    coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset(), stats=stats)
    assert stats["ordinary_folded_scans"] > SCANS / 2
    assert memo_of(plc).compiles == 1


def test_a_monitor_fires_on_the_scan_stepping_fires_it() -> None:
    plc = _runner(_ramp, 0.01)
    twin = plc.fork()
    seen: list[tuple[int, Any, Any]] = []
    expected: list[tuple[int, Any, Any]] = []
    plc.monitor(X, lambda now, before: seen.append((plc.current_state.scan_id, before, now)))
    twin.monitor(X, lambda now, before: expected.append((twin.current_state.scan_id, before, now)))
    coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
    twin.run(cycles=SCANS)
    assert seen == expected and len(seen) > 2


def test_a_tag_that_moves_without_a_write_keeps_the_coast_interpreted() -> None:
    clock = system.sys.clock_100ms
    plc = _runner(_ramp, 0.01)
    start = plc.current_state.scan_id
    stats: dict[str, int] = {}
    coast_until(
        plc,
        lambda _state: False,
        budget=SCANS,
        predicate_reads=frozenset((clock.name,)),
        stats=stats,
    )
    assert plc.current_state.scan_id == start + SCANS
    assert stats["logical_scans"] == SCANS
    assert stats["ordinary_folded_scans"] == 0


def test_a_saturated_timer_is_a_path_of_its_own() -> None:
    """The source is the same before and after the limit; what it moves is not."""

    def saturates() -> None:
        T = Timer.clone("SatT")
        with Rung():
            on_delay(T, 100)

    plc = _runner(saturates, 0.5)
    stats: dict[str, int] = {}
    coast_until(plc, lambda _s: False, budget=1000, predicate_reads=frozenset(), stats=stats)
    assert plc.current_state.tags["SatT_Acc"] == 32767
    # Saturation takes 66 scans, and the 900 after it stay in the loop as well.
    assert stats["kernel_scans"] < 100


def test_kernel_and_path_caps_hold(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coast_run, "MAX_PATHS", 2)
    plc = _runner(CASES["staircase_mirror"], 0.3)
    coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
    memo = memo_of(plc)
    assert all(len(held.paths) <= 2 for held in memo.paths.values())


def test_columns_kept_by_content_have_their_cap(monkeypatch: pytest.MonkeyPatch) -> None:
    uncapped = _runner(CASES["staircase_mirror"], 0.3)
    coast_until(uncapped, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
    assert max(len(held.interned) for held in memo_of(uncapped).paths.values()) > 2

    monkeypatch.setattr(coast_run, "MAX_INTERNED", 2)
    plc = _runner(CASES["staircase_mirror"], 0.3)
    coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
    assert all(len(held.interned) <= 2 for held in memo_of(plc).paths.values())
    # Past the cap two paths may no longer see they agree, and the answers are the same.
    twin = _runner(CASES["staircase_mirror"], 0.3)
    for _ in range(plc.state.scan_id - twin.state.scan_id):
        twin.step()
    compare_records("capped columns", plc, twin, 0)


def test_the_kernel_held_longest_unused_goes_first() -> None:
    memo = coast_run.CoastMemo()
    program = object()

    def runner_of(hold: object) -> Any:
        return SimpleNamespace(
            _compiled_replay_kernel=None,
            _synthesis=SimpleNamespace(plant=(), holds=(hold,)),
            _program=program,
            _compiled_replay_supported_kernel=object,
        )

    first, second, third = object(), object(), object()
    kept = coast_run._kernel_for(runner_of(first), memo)
    coast_run._kernel_for(runner_of(second), memo)
    assert coast_run._kernel_for(runner_of(first), memo) is kept
    coast_run._kernel_for(runner_of(third), memo)
    assert coast_run._kernel_for(runner_of(first), memo) is kept
    assert memo.compiles == 3
    assert len(memo.kernels) == coast_run.MAX_KERNELS == 2


@pytest.fixture
def _not_under_fold_check() -> None:
    # Lockstep compares every checkpoint and record with a stepped twin's, so a coast keeps
    # all of them there.  These read what a coast leaves out against stepping themselves.
    if getattr(compiled_run, "_fold_check_installed", False):
        pytest.skip("--fold-check coasts keep what stepping keeps")


class TestCheckpointsInAStretch:
    SCANS = 12_000

    pytestmark = pytest.mark.usefixtures("_not_under_fold_check")

    def _coasted(self, **kwargs: Any) -> PLC:
        plc = PLC(_program(_ramp), dt=0.01, **kwargs)
        plc.patch({A: False})
        plc.step()
        coast_until(plc, lambda _s: False, budget=self.SCANS, predicate_reads=frozenset())
        return plc

    def _stepped(self) -> PLC:
        plc = PLC(_program(_ramp), dt=0.01)
        plc.patch({A: False})
        for _ in range(self.SCANS + 1):
            plc.step()
        return plc

    def test_they_are_wider_than_stepping_leaves(self) -> None:
        plc = self._coasted()
        assert plc.state.scan_id > self.SCANS
        interval = plc._checkpoint_interval * coast_run.STRETCH_CHECKPOINT_INTERVALS
        assert len(plc._checkpoints) <= self.SCANS // interval + 3

    def test_a_thinned_history_reads_the_same(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(coast_run, "MAX_CHECKPOINTS", 3)
        plc = self._coasted()
        assert len(plc._checkpoints) <= 3
        stepped = self._stepped()
        for scan in (1, 150, 2500, 6001, 9999, self.SCANS):
            plc._recent_state_cache.clear()
            plc._replay_slabs.clear()
            _same_tags(dict(plc.history.at(scan).tags), dict(stepped.history.at(scan).tags))
        fork, twin = plc.fork_from(6001), stepped.fork_from(6001)
        for _ in range(5):
            fork.step()
            twin.step()
        _same_tags(dict(fork.state.tags), dict(twin.state.tags))

    def test_retention_holds_inside_a_stretch(self) -> None:
        plc = self._coasted(history=4000)
        assert plc.history.oldest_scan_id >= self.SCANS - 4000 - 2000
        assert len(plc._checkpoints) <= 4
        with pytest.raises(KeyError):
            plc.history.at(1)


class TestThinningCheckpoints:
    @staticmethod
    def _counting() -> Any:
        return _program(_count_while_forced)

    def test_a_force_changed_on_a_dropped_scan_is_still_known(self) -> None:
        plc, whole = (PLC(self._counting(), dt=0.1, checkpoint_interval=10) for _ in range(2))
        for each in (plc, whole):
            each.step()
            for scan in range(1, 80):
                # The change lands on checkpoint scans, the records a thinning weighs.
                if scan % 20 == 0:
                    each.force(A, scan % 40 == 0)
                each.step()
        forced_records = dict(plc._scan_log._force_changes_by_scan)
        assert len(plc._checkpoints) > 4
        plc._thin_checkpoints(4)
        assert len(plc._checkpoints) < len(whole._checkpoints)
        kept = plc._scan_log._force_changes_by_scan
        assert len(kept) < len(forced_records)
        for scan in range(1, plc.state.scan_id + 1):
            plc._recent_state_cache.clear()
            plc._replay_slabs.clear()
            whole._recent_state_cache.clear()
            whole._replay_slabs.clear()
            _same_tags(dict(plc.history.at(scan).tags), dict(whole.history.at(scan).tags))

    def test_a_record_that_only_changes_a_value_type_stays(self) -> None:
        plc = PLC(self._counting(), dt=0.1, checkpoint_interval=10)
        log = plc._scan_log
        log.record_force_changes(10, {"X": True})
        log.record_force_changes(20, {"X": 1})
        log.record_force_changes(30, {"X": 1})
        log.drop_force_records_unchanged_at({20, 30})
        assert sorted(log._force_changes_by_scan) == [10, 20]

    def test_a_plant_change_keeps_its_checkpoint(self) -> None:
        plc = PLC(self._counting(), dt=0.1, checkpoint_interval=10)
        for _ in range(81):
            plc.step()
        plc._plant_changes = [(20, [])]
        plc._thin_checkpoints(2)
        assert 20 in plc._checkpoints
        assert 40 not in plc._checkpoints or 60 not in plc._checkpoints


Toggle = Bool("AdvToggle")


def _fires_every_other_scan() -> None:
    with Rung(~Toggle):
        out(Toggle)
    with Rung(Toggle):
        calc(X + 1, X)
    with Rung():
        on_delay(T, 30000)


class TestRecordsPastTheirCap:
    pytestmark = pytest.mark.usefixtures("_not_under_fold_check")

    SCANS = 12_000

    def _coasted(self, monkeypatch: pytest.MonkeyPatch, runs: int) -> PLC:
        monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", runs)
        plc = _runner(_fires_every_other_scan, 0.01)
        coast_until(plc, lambda _s: False, budget=self.SCANS, predicate_reads=frozenset())
        return plc

    def _stepped(self, plc: PLC) -> PLC:
        stepped = _runner(_fires_every_other_scan, 0.01)
        while stepped.state.scan_id < plc.state.scan_id:
            stepped.step()
        return stepped

    def test_they_stop_growing_and_read_the_same(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plc = self._coasted(monkeypatch, 200)
        held = plc._rung_firing_timelines.run_count()
        assert held < self.SCANS / 3
        assert plc._fold_windows
        stepped = self._stepped(plc)
        tip = plc.state.scan_id
        assert plc.query.cold_rungs() == stepped.query.cold_rungs()
        assert plc.query.hot_rungs() == stepped.query.hot_rungs()
        for scan in (tip - 7, tip - 1, tip):
            assert str(plc.cause(Toggle, scan)) == str(stepped.cause(Toggle, scan))
        assert not plc._fold_windows
        compare_records("records past the cap", plc, stepped, 0)

    def test_one_scan_far_from_a_checkpoint_counts_the_run_up(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        plc = self._coasted(monkeypatch, 200)
        tip = plc.state.scan_id
        first, last = plc._fold_windows[-1]
        plc._checkpoints.clear()
        monkeypatch.setattr(runner, "SETTLE_SCAN_LIMIT", last - plc._initial_scan_id - 1)
        with pytest.raises(UnrecordedScansError) as refused:
            plc.rung_firings(last)
        assert refused.value.scans == last - plc._initial_scan_id
        assert tip == plc.state.scan_id
        assert plc._fold_windows[-1] == (first, last)

    def test_a_settle_polls_the_searchs_limits(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plc = self._coasted(monkeypatch, 200)
        first, last = plc._fold_windows[-1]
        plc._checkpoints.clear()
        ran_out = RuntimeError("out of time")
        polls = {"n": 0}

        def check() -> None:
            polls["n"] += 1
            if polls["n"] == 5:
                raise ran_out

        memo_of(plc).check_limits = check
        with pytest.raises(RuntimeError, match="out of time"):
            plc.rung_firings(last)
        assert polls["n"] == 5
        # Nothing of it was kept, and a later question can still ask.
        assert plc._fold_windows[-1] == (first, last)

    def test_too_far_to_settle_is_a_named_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        plc = self._coasted(monkeypatch, 200)
        monkeypatch.setattr(runner, "SETTLE_SCAN_LIMIT", 10)
        with pytest.raises(UnrecordedScansError):
            plc.query.cold_rungs()
