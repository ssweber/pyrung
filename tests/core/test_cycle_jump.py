"""A cycle prediction says how many cycles run as the next one does, and stepping agrees.

The operations on forms are checked against the same operation on each cycle's value.  In
shadow mode each long run checks every prediction against the scans the loop then runs.  Each
run that jumps is checked against a stepped twin, on its state and its records.
"""

from __future__ import annotations

import random
import tracemalloc
from collections.abc import Callable
from datetime import datetime
from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    Timer,
    coast_run,
    copy,
    count_up,
    cycle_jump,
    latch,
    math,
    on_delay,
    out,
    scan_trace,
    system,
)
from pyrung.core.coast_run import coast_until, memo_of
from pyrung.core.cycle_jump import Lin, _bin, _Bound, _call, _cmp, _value_at
from tests import fold_check
from tests.fold_check import compare_records

HORIZON = 4095


def _bound() -> _Bound:
    return _Bound(HORIZON)


class TestForms:
    def test_an_ordering_keeps_the_cycles_before_it_changes(self) -> None:
        bound = _bound()
        assert _cmp(bound, "lt", Lin(0, 3), 10) is True
        assert bound.last == 3  # 9 < 10, and cycle 4 gives 12

    def test_an_equality_keeps_the_cycles_before_it_is_met(self) -> None:
        bound = _bound()
        assert _cmp(bound, "eq", Lin(0, 2), 6) is False
        assert bound.last == 2

    def test_an_equality_never_met_keeps_every_cycle(self) -> None:
        bound = _bound()
        assert _cmp(bound, "eq", Lin(1, 2), 6) is False
        assert bound.last == HORIZON

    def test_a_floor_division_that_does_not_divide_the_step_keeps_one_quotient(self) -> None:
        # The RTC hour of a timestamp in nanoseconds, one second a cycle.
        bound = _bound()
        assert _bin(bound, "floordiv", Lin(40_110_000_000, 1_000_000_000), 3_600_000_000_000) == 0
        assert bound.last == 3559

    def test_a_floor_division_by_a_divisor_of_the_step_is_a_form(self) -> None:
        bound = _bound()
        quotient = _bin(bound, "floordiv", Lin(10, 6), 3)
        assert (quotient.a, quotient.b) == (3, 2)
        assert _bin(bound, "mod", Lin(10, 6), 3) == 1
        assert bound.last == HORIZON

    def test_a_remainder_keeps_the_cycles_with_one_quotient(self) -> None:
        bound = _bound()
        remainder = _bin(bound, "mod", Lin(7, 2), 5)
        assert (remainder.a, remainder.b) == (2, 2)
        assert bound.last == 1

    def test_min_keeps_the_cycles_before_the_other_side_wins(self) -> None:
        bound = _bound()
        assert _value_at(_call(bound, "min", Lin(0, 1), 5), 0) == 0
        assert bound.last == 5  # min keeps the first while the two are equal

    def test_a_form_divided_by_a_form_declines(self) -> None:
        with pytest.raises(cycle_jump.Unsupported):
            _bin(_bound(), "floordiv", Lin(7, 2), Lin(3, 1))


def _concrete(name: str, x: int, constant: int) -> Any:
    if name in ("lt", "le", "gt", "ge", "eq", "ne"):
        return {
            "lt": x < constant,
            "le": x <= constant,
            "gt": x > constant,
            "ge": x >= constant,
            "eq": x == constant,
            "ne": x != constant,
        }[name]
    if name == "floordiv":
        return x // constant
    if name == "mod":
        return x % constant
    if name == "and_":
        return x & constant
    if name == "min":
        return min(x, constant)
    if name == "max":
        return max(constant, x)
    return abs(x)


def _on_form(name: str, bound: _Bound, x: Lin, constant: int) -> Any:
    if name in ("lt", "le", "gt", "ge", "eq", "ne"):
        return _cmp(bound, name, x, constant)
    if name in ("floordiv", "mod", "and_"):
        return _bin(bound, name, x, constant)
    if name == "min":
        return _call(bound, "min", x, constant)
    if name == "max":
        return _call(bound, "max", constant, x)
    return _call(bound, "abs", x)


@pytest.mark.parametrize(
    "name", ["lt", "le", "gt", "ge", "eq", "ne", "floordiv", "mod", "and_", "min", "max", "abs"]
)
def test_each_operation_on_a_form_is_the_value_on_every_cycle_it_keeps(name: str) -> None:
    rng = random.Random(name)
    for _ in range(300):
        x = Lin(rng.randint(-500, 500), rng.choice([-7, -3, -1, 1, 2, 5, 11]))
        constant = rng.choice([-9, -4, 3, 7, 15, 64, 255, rng.randint(-600, 600) or 1])
        bound = _Bound(200)
        try:
            result = _on_form(name, bound, x, constant)
        except cycle_jump.Unsupported:
            continue
        for k in range(bound.last + 1):
            assert _value_at(result, k) == _concrete(name, x.at(k), constant), (x, constant, k)


Toggle = Bool("CycToggle")
Count = Int("CycCount")
Sec = Int("CycSec")
Hit = Bool("CycHit")
Ramp = Real("CycRamp")
T = Timer.clone("CycT")


def _timer_flip() -> None:
    with Rung(~Toggle):
        out(Toggle)
    with Rung(Toggle):
        math(Count + 1, Count)
    with Rung():
        on_delay(T, 30000)
    with Rung(T.Done):
        latch(Hit)


def _rtc_flip() -> None:
    with Rung(~Toggle):
        out(Toggle)
    with Rung(Toggle):
        math(Count + 1, Count)
    with Rung():
        copy(system.rtc.second, Sec)
    with Rung(Sec == 30):
        latch(Hit)


def _real_flip() -> None:
    with Rung(~Toggle):
        out(Toggle)
    with Rung(Toggle):
        math(Ramp + 0.5, Ramp)


Small = Int("CycSmall")
Counted = Bool("CycCounted")
Clear = Bool("CycClear")
C = Counter.clone("CycC")


def _clamp_flip() -> None:
    with Rung(~Toggle):
        out(Toggle)
    with Rung(Toggle):
        math(Count + 1, Count)
    with Rung():
        copy(Count * 10, Small)


def _counter_flip() -> None:
    with Rung(~Toggle):
        out(Toggle)
    with Rung(Toggle):
        math(Count + 1, Count)
    with Rung(Toggle):
        count_up(C, preset=3000).reset(Clear)
    with Rung(C.Done):
        latch(Counted)


def _runner(build: Any, dt: float) -> PLC:
    with Program(strict=False) as program:
        build()
    plc = PLC(program, dt=dt)
    plc.set_rtc(datetime(2026, 1, 2, 3, 4, 5))
    plc.step()
    return plc


@pytest.fixture
def shadow(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coast_run, "CYCLE_PREDICTIONS", "shadow")
    monkeypatch.setattr(coast_run, "STRICT_CYCLE_CHECK", True)


@pytest.fixture
def jump(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coast_run, "CYCLE_PREDICTIONS", "jump")
    monkeypatch.setattr(coast_run, "STRICT_CYCLE_CHECK", False)


def _coast_and_step(
    build: Any,
    dt: float,
    stop_at: int | None,
    budget: int,
    reached: Callable[[Any], bool] | None = None,
) -> PLC:
    """Coast *build* until Count reaches *stop_at*, and check a stepped twin lands alike.

    *reached*, if given, is the stop predicate instead, and reads Count alone.
    """
    plc = _runner(build, dt)
    twin = plc.fork()
    start = plc.current_state.scan_id

    if reached is None:

        def reached(state: Any) -> bool:
            return stop_at is not None and state.tags[Count.name] >= stop_at

        reads = frozenset((Count.name,)) if stop_at is not None else frozenset()
    else:
        reads = frozenset((Count.name,))
    coast_until(plc, reached, budget=budget, predicate_reads=reads)
    while twin.current_state.scan_id < plc.current_state.scan_id and not reached(
        twin.current_state
    ):
        twin.step()
    _assert_lands_alike(plc, twin)
    compare_records("cycles", plc, twin, start)
    return plc


def _assert_lands_alike(plc: PLC, twin: PLC) -> None:
    assert twin.current_state.scan_id == plc.current_state.scan_id
    assert twin.current_state.timestamp == plc.current_state.timestamp
    assert dict(twin.current_state.tags) == dict(plc.current_state.tags)
    assert dict(twin.current_state.memory) == dict(plc.current_state.memory)


@pytest.mark.usefixtures("shadow")
class TestShadow:
    def test_a_timer_under_a_flip_flop_is_predicted_up_to_the_stop(self) -> None:
        plc = _coast_and_step(_timer_flip, 0.01, stop_at=6000, budget=20_000)
        counts = memo_of(plc).counts
        assert counts["cycle predictions"] >= 1
        assert plc.current_state.tags[Count.name] == 6000

    def test_the_rtc_second_is_predicted_up_to_the_minute(self) -> None:
        plc = _coast_and_step(_rtc_flip, 0.5, stop_at=None, budget=6000)
        counts = memo_of(plc).counts
        assert counts["cycle predictions"] >= 1
        # A minute is 60 cycles, so a prediction never runs past one.
        assert counts["cycles predicted"] <= 60 * counts["cycle predictions"]

    def test_a_real_that_moves_declines(self) -> None:
        plc = _coast_and_step(_real_flip, 0.01, stop_at=None, budget=6000)
        counts = memo_of(plc).counts
        assert counts["cycle predictions"] == 0
        assert counts[
            "cycle prediction declined: a value other than a whole number changes over a cycle"
        ]


@pytest.mark.usefixtures("jump")
class TestJump:
    def test_a_jump_stops_where_the_stop_predicate_does(self) -> None:
        plc = _coast_and_step(_timer_flip, 0.01, stop_at=6000, budget=20_000)
        assert memo_of(plc).counts["cycles jumped"] > 0
        assert plc.current_state.tags[Count.name] == 6000

    def test_a_jump_over_the_rtc_second_lands_as_stepping(self) -> None:
        plc = _coast_and_step(_rtc_flip, 0.5, stop_at=None, budget=6000)
        assert memo_of(plc).counts["cycles jumped"] > 0

    def test_a_wrong_prediction_never_jumps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(scan_trace, "SELF_CHECK_OBSERVERS", [])
        _wrong_by_one(monkeypatch)
        plc = _coast_and_step(_timer_flip, 0.01, stop_at=None, budget=20_000)
        counts = memo_of(plc).counts
        assert counts["cycle prediction mismatches"] >= 1
        assert counts["cycle jumps"] == 0

    def test_a_clamp_inside_the_predicted_cycles_lands_as_stepping(self) -> None:
        plc = _coast_and_step(_clamp_flip, 0.01, stop_at=None, budget=20_000)
        assert memo_of(plc).counts["cycles jumped"] > 0
        # Count * 10 passes the most an Int holds at Count 3277.
        assert plc.current_state.tags[Small.name] == 32767

    def test_a_counter_reaches_its_preset_as_stepping_does(self) -> None:
        plc = _coast_and_step(_counter_flip, 0.01, stop_at=None, budget=20_000)
        assert memo_of(plc).counts["cycles jumped"] > 0
        tags = plc.current_state.tags
        assert tags[C.Acc.name] == tags[Count.name] > 3000
        assert tags[Counted.name] is True

    def test_a_stop_predicate_true_on_one_value_alone_stops_there(self) -> None:
        def reached(state: Any) -> bool:
            count = state.tags[Count.name]
            return count >= 3000 and count % 1000 == 777

        plc = _coast_and_step(_timer_flip, 0.01, stop_at=None, budget=20_000, reached=reached)
        assert memo_of(plc).counts["cycles jumped"] > 0
        assert plc.current_state.tags[Count.name] == 3777

    def test_seek_and_fork_after_a_jump_read_as_stepping(self) -> None:
        plc = _coast_and_step(_timer_flip, 0.01, stop_at=None, budget=20_000)
        assert memo_of(plc).counts["cycles jumped"] > 0
        twin = _runner(_timer_flip, 0.01)
        while twin.current_state.scan_id < plc.current_state.scan_id:
            twin.step()
        for scan_id in (4_321, 12_345, 19_999):
            ours, theirs = plc.seek(scan_id), twin.seek(scan_id)
            assert dict(ours.tags) == dict(theirs.tags)
            assert dict(ours.memory) == dict(theirs.memory)
            ours_fork, theirs_fork = plc.fork(scan_id), twin.fork(scan_id)
            for _ in range(50):
                ours_fork.step()
                theirs_fork.step()
            _assert_lands_alike(ours_fork, theirs_fork)

    def test_a_path_keeps_few_compiled_iterations(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(cycle_jump, "MAX_VARIANTS", 1)
        made: list[cycle_jump.Iteration] = []
        iteration_of = cycle_jump.iteration_of

        def noted(shape: Any) -> cycle_jump.Iteration:
            made.append(iteration_of(shape))
            return made[-1]

        monkeypatch.setattr(cycle_jump, "iteration_of", noted)
        compiled: list[frozenset[str]] = []
        variant = cycle_jump.Iteration._variant

        def counted(self: cycle_jump.Iteration, moving: frozenset[str]) -> Any:
            compiled.append(moving)
            return variant(self, moving)

        monkeypatch.setattr(cycle_jump.Iteration, "_variant", counted)
        plc = _coast_and_step(_timer_flip, 0.01, stop_at=6000, budget=20_000)
        assert memo_of(plc).counts["cycles jumped"] > 0
        # Each prediction runs a path on values and then on forms, so one kept is too few.
        assert len(compiled) > len(set(compiled))
        assert all(len(iteration._variants) <= 1 for iteration in made)


def _wrong_by_one(monkeypatch: pytest.MonkeyPatch) -> None:
    predict = cycle_jump.predict

    def wrong(*args: Any, **kwargs: Any) -> Any:
        found = predict(*args, **kwargs)
        if not isinstance(found, str):
            found.steps = {cell: step + 1 for cell, step in found.steps.items()}
        return found

    monkeypatch.setattr(cycle_jump, "predict", wrong)


def test_a_wrong_prediction_fails_a_strict_check(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(coast_run, "CYCLE_PREDICTIONS", "shadow")
    monkeypatch.setattr(coast_run, "STRICT_CYCLE_CHECK", True)
    monkeypatch.setattr(scan_trace, "SELF_CHECK_OBSERVERS", [fold_check._note_self_check_mismatch])
    _wrong_by_one(monkeypatch)
    plc = _runner(_timer_flip, 0.01)
    with fold_check.deliberate(), pytest.raises(coast_run.CycleMismatch):
        coast_until(plc, lambda _s: False, budget=20_000, predicate_reads=frozenset())


def test_a_wrong_prediction_is_counted_and_the_run_still_steps(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(coast_run, "CYCLE_PREDICTIONS", "shadow")
    monkeypatch.setattr(coast_run, "STRICT_CYCLE_CHECK", False)
    # Each prediction after the first misses as well, and --fold-check must count none.
    monkeypatch.setattr(scan_trace, "SELF_CHECK_OBSERVERS", [])
    _wrong_by_one(monkeypatch)
    plc = _coast_and_step(_timer_flip, 0.01, stop_at=None, budget=20_000)
    assert memo_of(plc).counts["cycle prediction mismatches"] >= 1


def _counting_run(mode: str, monkeypatch: pytest.MonkeyPatch) -> tuple[int, list[int]]:
    """Run a predicate that reads a tag moving every scan, in *mode*.

    It gives the run's peak memory and the asks of each prediction.
    """
    monkeypatch.setattr(coast_run, "CYCLE_PREDICTIONS", mode)
    monkeypatch.setattr(coast_run, "STRICT_CYCLE_CHECK", False)
    # The loop runs in long bursts, as it does outside --fold-check.
    monkeypatch.setattr(scan_trace, "BURST_SIZES", None)
    asks: list[int] = []
    predict = cycle_jump.predict

    def counted(
        steps: Any,
        read_cell: Any,
        ts: Any,
        dt: Any,
        rtc: Any,
        horizon: Any,
        stops: Any,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        asks.append(0)

        def ask(values: Any) -> bool:
            asks[-1] += 1
            return stops(values)

        return predict(steps, read_cell, ts, dt, rtc, horizon, ask, *args, **kwargs)

    monkeypatch.setattr(cycle_jump, "predict", counted)
    moving = Dint("CycMoving")
    timer = Timer.clone("CycAsked")
    with Program(strict=False) as program:
        with Rung(~timer.Done):
            on_delay(timer, 1000)
        with Rung():
            copy(moving + 1, moving)
    plc = PLC(program, dt=0.01)
    plc.step()
    tracemalloc.start()
    try:
        # Under --fold-check the public call steps a twin through every scan of both runs.
        coast_run._coast_until(
            plc,
            lambda state: state.tags[moving.name] >= 2_000_000_000,
            300_000,
            frozenset((moving.name,)),
            None,
            None,
            1,
            None,
        )
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert plc.current_state.tags[moving.name] == 300_001
    return peak, asks


def test_a_predicate_on_a_tag_that_moves_costs_about_what_stepping_costs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    off_peak, _ = _counting_run("off", monkeypatch)
    peak, asks = _counting_run("jump", monkeypatch)
    assert asks
    # Each prediction asks about the moving tag up to the cap, and once per scan of cycle 0.
    assert max(asks) <= cycle_jump.MAX_ASKS + 2 * coast_run.MAX_PREDICTED_PERIOD
    assert peak <= 4 * off_peak + 8_000_000, (peak, off_peak)
