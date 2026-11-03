"""A coast lands where stepping lands, whatever the program does between its scans.

Each program moves something in a way a scan's trace might describe wrongly, and each
coast runs next to a stepped twin that must match it after every loop firing.
"""

from __future__ import annotations

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
    compiled_run,
    copy,
    latch,
    on_delay,
    out,
    reset,
    rise,
    scan_trace,
    system,
)
from pyrung.core.coast_run import coast_until, memo_of
from pyrung.core.tag import Dint

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
        assert stats["kernel_scans"] + stats["ordinary_folded_scans"] == SCANS

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
    from pyrung.core import coast_run

    monkeypatch.setattr(coast_run, "MAX_PATHS", 2)
    plc = _runner(CASES["staircase_mirror"], 0.3)
    coast_until(plc, lambda _s: False, budget=SCANS, predicate_reads=frozenset())
    memo = memo_of(plc)
    assert all(len(held.paths) <= 2 for held in memo.paths.values())
