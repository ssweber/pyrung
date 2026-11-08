"""A cycle prediction says how many cycles run as the next one does, and stepping agrees.

The operations on forms are checked against the same operation on each cycle's value.  The
coasts run with every prediction checked against the scans the loop then runs.
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Real,
    Rung,
    Timer,
    coast_run,
    copy,
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


def _coast_and_step(build: Any, dt: float, stop_at: int | None, budget: int) -> PLC:
    """Coast *build* until Count reaches *stop_at*, and check a stepped twin lands alike."""
    plc = _runner(build, dt)
    twin = plc.fork()
    start = plc.current_state.scan_id

    def reached(state: Any) -> bool:
        return stop_at is not None and state.tags[Count.name] >= stop_at

    reads = frozenset((Count.name,)) if stop_at is not None else frozenset()
    coast_until(plc, reached, budget=budget, predicate_reads=reads)
    while twin.current_state.scan_id < plc.current_state.scan_id and not reached(
        twin.current_state
    ):
        twin.step()
    assert twin.current_state.scan_id == plc.current_state.scan_id
    assert twin.current_state.timestamp == plc.current_state.timestamp
    assert dict(twin.current_state.tags) == dict(plc.current_state.tags)
    compare_records("cycles", plc, twin, start)
    return plc


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
