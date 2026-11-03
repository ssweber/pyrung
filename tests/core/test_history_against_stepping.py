"""History rebuilds the scans as they ran.

A seek re-executes from a checkpoint and the scan log, on a kernel or interpreted.  Each
test here holds what a seek returns against states kept as they were stepped, on the
runner that stepped them as well as on one that ran the same scans compiled: a seek that
is wrong by itself must not pass by agreeing with itself.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

import pytest

from pyrung import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    copy,
    on_delay,
    out,
    rise,
    run_function,
)
from pyrung.core import compiled_run, scan_trace
from pyrung.core.scan_log import ScanLog
from pyrung.core.synthesis import Synthesis, function_rung
from pyrung.core.system_points import system
from pyrung.core.time_mode import TimeMode
from tests import fold_check

Run, Lamp = Bool("HsRun"), Bool("HsLamp")
N, Hour, Second = Int("HsN"), Int("HsHour"), Int("HsSecond")
T, T1 = Timer.clone("HsT"), Timer.clone("HsT1")

RTC_A = datetime(2026, 3, 14, 15, 9, 26, 500000)
RTC_B = datetime(2031, 7, 2, 4, 40, 1)


@pytest.fixture
def fine_loop(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(compiled_run, "INTERPRETED_PREFIX", 1)
    monkeypatch.setattr(scan_trace, "MIN_RUN_SCANS", 1)
    monkeypatch.setattr(scan_trace, "BURST_SIZES", (1, 2, 3, 5, 8))
    monkeypatch.setattr(scan_trace, "STRICT_SELF_CHECK", True)


@pytest.fixture(params=["compiled", "interpreted"])
def replay_path(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> str:
    """Seek on the kernel, or with none: both paths have to rebuild the same scans."""
    if request.param == "interpreted":
        monkeypatch.setattr(PLC, "_compiled_replay_supported_kernel", lambda _self: None)
    return request.param


def _scan(plc: PLC, scans: int, keep: dict[int, Any]) -> None:
    """One run: a stop a rung asks for inside it takes effect at the next call."""
    plc._ensure_running()
    for _ in range(scans):
        plc._run_single_scan(consume_pause_request=False)
        keep[plc.state.scan_id] = plc.state


def _assert_history(plc: PLC, truth: dict[int, Any], scans: Iterable[int] | None = None) -> None:
    plc._recent_state_cache.clear()
    plc._replay_slabs.clear()
    for scan in sorted(truth) if scans is None else scans:
        fold_check._compare("history", scan, plc.history.at(scan), truth[scan])


# --- A stop a rung asks for ------------------------------------------------------------


def _stops_itself() -> Program:
    with Program(strict=False) as program:
        with Rung(Run):
            on_delay(T, 1_000)
        with Rung(T.Done):
            out(system.sys.cmd_mode_stop)
        with Rung(Run):
            out(Lamp)
        with Rung():
            calc(N + 1, N)
    return program


def test_a_seek_walks_past_a_rung_stop(replay_path: str) -> None:
    """A rung stop scans on until the next call.  A replay of them must not restart."""
    plc = PLC(_stops_itself(), dt=0.01)
    plc.patch({Run: True})
    truth: dict[int, Any] = {}
    _scan(plc, 450, truth)  # checkpoints at 200 and 400, both past the stop at 101
    assert plc._running is False
    assert truth[450].tags["HsN"] == 450
    assert sorted(plc._checkpoints) == [200, 400]
    _assert_history(plc, truth, [*range(1, 450, 37), 99, 100, 101, 102, 200, 201, 400, 401, 450])


def test_a_restart_starts_a_new_recording(replay_path: str) -> None:
    """Scan ids start over at a restart, so the old run's patches would replay under them."""
    plc = PLC(_stops_itself(), dt=0.01)
    plc.patch({Run: True})
    first: dict[int, Any] = {}
    _scan(plc, 150, first)
    second: dict[int, Any] = {}
    _scan(plc, 450, second)
    assert plc.state.scan_id == 450
    assert second[1].tags["HsRun"] is False
    assert not plc._scan_log.snapshot().patches_by_scan
    _assert_history(plc, second, [*range(1, 450, 41), 200, 400, 450])


def test_a_stop_inside_a_compiled_range_seeks_like_stepping(fine_loop: None) -> None:
    plc, twin = PLC(_stops_itself(), dt=0.01), PLC(_stops_itself(), dt=0.01)
    plc.patch({Run: True})
    twin.patch({Run: True})
    before = scan_trace.TOTALS["loop scans"]
    plc.run_for(4.5)
    assert scan_trace.TOTALS["loop scans"] > before
    truth: dict[int, Any] = {}
    _scan(twin, plc.state.scan_id, truth)
    assert plc._running is False
    assert twin._running is False
    samples = [*range(1, plc.state.scan_id, 37), 101, 200, 400, plc.state.scan_id]
    _assert_history(plc, truth, samples)
    _assert_history(twin, truth, samples)


# --- The RTC ---------------------------------------------------------------------------


def _reads_the_clock(apply_after_ms: int = 30_000) -> Program:
    with Program(strict=False) as program:
        with Rung(Run):
            on_delay(T, apply_after_ms)
        with Rung(rise(T.Done)):
            copy(10, system.rtc.new_second)
            copy(5, system.rtc.new_minute)
            copy(1, system.rtc.new_hour)
            out(system.rtc.apply_time)
        with Rung():
            copy(system.rtc.hour, Hour)
        with Rung():
            copy(system.rtc.second, Second)
    return program


def test_history_before_a_later_clock_change_keeps_the_old_clock(replay_path: str) -> None:
    """A seek starts from the base of the scan it starts at, not the current one."""
    plc = PLC(_reads_the_clock(), dt=0.01)
    plc.set_rtc(RTC_A)
    truth: dict[int, Any] = {}
    _scan(plc, 250, truth)
    plc.set_rtc(RTC_B)
    _scan(plc, 250, truth)
    assert truth[10].tags["HsHour"] == RTC_A.hour
    assert truth[490].tags["HsHour"] == RTC_B.hour
    _assert_history(plc, truth, [1, 10, 199, 200, 201, 250, 251, 252, 399, 400, 401, 500])


def test_rtc_at_a_scan_before_a_later_clock_change(replay_path: str) -> None:
    plc = PLC(_reads_the_clock(), dt=0.01)
    plc.set_rtc(RTC_A)
    _scan(plc, 250, {})
    plc.set_rtc(RTC_B)
    _scan(plc, 250, {})
    assert plc._replay_rtc_at_state(plc.history.at(100), plc._scan_log).hour == RTC_A.hour
    assert plc._replay_rtc_at_state(plc.history.at(400), plc._scan_log).hour == RTC_B.hour


def test_an_rtc_apply_inside_a_range_reaches_runner_log_history_and_forks() -> None:
    """Production settings: the apply falls in the loop's range, past the prefix."""
    program = _reads_the_clock()
    plc, twin = PLC(program, dt=0.01), PLC(program, dt=0.01)
    for runner in (plc, twin):
        runner.set_rtc(RTC_A)
        runner.patch({Run: True})
    plc.step()
    truth: dict[int, Any] = {}
    twin.step()
    before = scan_trace.TOTALS["loop scans"]
    plc.run_for(80.0)
    assert scan_trace.TOTALS["loop scans"] > before
    _scan(twin, plc.state.scan_id - twin.state.scan_id, truth)
    fold_check._compare("compiled", plc.state.scan_id, plc.state, twin.state)

    assert plc._rtc_base == twin._rtc_base
    assert plc._rtc_base_sim_time == twin._rtc_base_sim_time
    assert plc._scan_log.snapshot().rtc_base_changes == twin._scan_log.snapshot().rtc_base_changes
    assert len(plc._scan_log.snapshot().rtc_base_changes) == 2  # set_rtc, then the apply

    applied = max(plc._scan_log.snapshot().rtc_base_changes)
    samples = [500, 1999, 2000, applied - 1, applied, applied + 1, 3100, 4000, 6500, 7999]
    _assert_history(plc, truth, samples)
    _assert_history(twin, truth, samples)
    for scan in (2000, 3100, 6500):
        fork, twin_fork = plc.fork_from(scan), twin.fork_from(scan)
        fold_check._compare("history", scan, fork.state, truth[scan])
        fork.run(50)
        twin_fork.run(50)
        fold_check._compare("history", scan + 50, fork.state, twin_fork.state)


def test_an_apply_of_an_undone_scan_goes_with_it(
    monkeypatch: pytest.MonkeyPatch, fine_loop: None
) -> None:
    """A scan that raises is undone and stepped again: its apply must land only once."""
    Bounded = Int("HsBounded", min=0, max=10)

    def fill() -> None:
        with Rung(Run):
            on_delay(T, 500)
        with Rung(rise(T.Done)):
            copy(1, system.rtc.new_hour)
            copy(5, system.rtc.new_minute)
            copy(10, system.rtc.new_second)
            out(system.rtc.apply_time)
        with Rung(T.Done):
            copy(99, Bounded)
        with Rung():
            copy(system.rtc.hour, Hour)

    with Program(strict=False) as program:
        fill()
    plc, twin = PLC(program, dt=0.01), PLC(program, dt=0.01)
    for runner in (plc, twin):
        runner.set_rtc(RTC_A)
        runner.patch({Run: True})
    with pytest.warns(UserWarning):
        plc.run_for(1.0)
    with pytest.warns(UserWarning):
        _scan(twin, plc.state.scan_id, {})
    fold_check._compare("compiled", plc.state.scan_id, plc.state, twin.state)
    assert plc._rtc_base == twin._rtc_base
    assert plc._scan_log.snapshot().rtc_base_changes == twin._scan_log.snapshot().rtc_base_changes


def test_a_trim_keeps_the_clock_of_the_scans_it_keeps() -> None:
    """The entry before a horizon is what the kept scans before the next one read."""
    log = ScanLog(time_mode=TimeMode.FIXED_STEP, base_scan=0)
    start = RTC_A - timedelta(days=1)
    log.record_rtc_base_change(10, RTC_A, 0.1, previous=(start, 0.0))
    log.record_rtc_base_change(100, RTC_B, 1.0, previous=(RTC_A, 0.1))
    assert log.rtc_base_at(5) == (start, 0.0)
    assert log.rtc_base_at(10) == (RTC_A, 0.1)
    assert log.rtc_base_at(99) == (RTC_A, 0.1)
    assert log.rtc_base_at(100) == (RTC_B, 1.0)

    log.trim_before(50)
    assert log.rtc_base_at(50) == (RTC_A, 0.1)
    assert log.rtc_base_at(99) == (RTC_A, 0.1)
    assert log.rtc_base_at(100) == (RTC_B, 1.0)
    assert log.snapshot().rtc_base_at(60) == (RTC_A, 0.1)


# --- A key a rung, patch or force puts in the state ------------------------------------


def _new_second_at_its_default() -> Program:
    with Program(strict=False) as program:
        with Rung(Run):
            on_delay(T, 1_000)
        with Rung(T.Done):
            copy(0, system.rtc.new_second)
        with Rung(system.rtc.new_second == 0):
            out(Lamp)
    return program


def _only_reads_new_second() -> Program:
    with Program(strict=False) as program:
        with Rung(system.rtc.new_second == 0):
            out(Lamp)
        with Rung():
            calc(N + 1, N)
    return program


def test_a_seek_adds_a_key_a_rung_stored_its_default_in(replay_path: str) -> None:
    plc = PLC(_new_second_at_its_default(), dt=0.01)
    plc.patch({Run: True})
    truth: dict[int, Any] = {}
    _scan(plc, 250, truth)
    assert "rtc.new_second" not in truth[50].tags
    assert truth[150].tags["rtc.new_second"] == 0
    _assert_history(plc, truth, [50, 100, 101, 102, 150, 200, 201, 250])


def test_a_seek_adds_a_key_a_patch_stored_its_default_in(replay_path: str) -> None:
    plc = PLC(_only_reads_new_second(), dt=0.01)
    truth: dict[int, Any] = {}
    _scan(plc, 30, truth)
    plc.patch({system.rtc.new_second: 0})
    _scan(plc, 30, truth)
    assert "rtc.new_second" not in truth[30].tags
    assert truth[31].tags["rtc.new_second"] == 0
    _assert_history(plc, truth, [30, 31, 32, 60])


@pytest.mark.parametrize(("value", "adds"), [(0, False), (5, True)])
def test_a_seek_of_a_forced_absent_tag(replay_path: str, value: int, adds: bool) -> None:
    """A force writes only what differs, so one at the default adds no key."""
    plc = PLC(_only_reads_new_second(), dt=0.01)
    truth: dict[int, Any] = {}
    _scan(plc, 30, truth)
    plc.force(system.rtc.new_second, value)
    _scan(plc, 30, truth)
    assert "rtc.new_second" not in truth[30].tags
    assert ("rtc.new_second" in truth[31].tags) is adds
    _assert_history(plc, truth, [30, 31, 32, 60])


def test_a_seek_of_a_rung_that_reads_the_scan_time_minimum(replay_path: str) -> None:
    """The first scan's hook starts it from the scan time, or a kernel leaves it at 0."""
    with Program(strict=False) as program:
        with Rung():
            copy(system.sys.scan_time_min_ms, N)
    plc = PLC(program, dt=0.01)
    truth: dict[int, Any] = {}
    _scan(plc, 30, truth)
    assert truth[30].tags["HsN"] == 10
    _assert_history(plc, truth, [1, 2, 12, 30])


# --- A run_function, which a kernel holds as copied source ----------------------------


GLOBAL_OFFSET = 7
global_calls = 0


def _pure(value: int) -> dict[str, int]:
    return {"out": abs(value) + 1}


def _reads_a_global(value: int) -> dict[str, int]:
    return {"out": value + GLOBAL_OFFSET}


def _stores_a_global(value: int) -> dict[str, int]:
    global global_calls
    global_calls += 1
    return {"out": value + 1}


def _closes_over() -> tuple[Callable[..., dict[str, int]], dict[str, int]]:
    calls = {"count": 0}

    def impure(value: int) -> dict[str, int]:
        calls["count"] += 1
        return {"out": value + 1}

    return impure, calls


def _passes_through(fn: Callable[..., Any]) -> Callable[..., Any]:
    return fn


@_passes_through
def _decorated(value: int) -> dict[str, int]:
    return {"out": value + 1}


class _UserType:
    pass


def _annotated_with_a_user_type(value: int, unused: _UserType | None = None) -> dict[str, int]:
    """This module's annotations are strings, so the copied def does not read them."""
    return {"out": value + 1}


def _default_names_a_global(value: int, offset: int = GLOBAL_OFFSET) -> dict[str, int]:
    """The default is read once, at the def: a kernel calling the function has its value."""
    return {"out": value + offset}


def _default_is_an_expression(value: int, offset: int = len("abc")) -> dict[str, int]:
    """Evaluated once, at the def, and what it left is a constant."""
    return {"out": value + offset}


Counted = Int("HsCounted")
Never = Bool("HsNever")


def _keeps_state_in_a_default(value: int, memo: list[int] = []) -> dict[str, int]:  # noqa: B006
    memo.append(value)
    return {"out": len(memo)}


def _keeps_state_in_a_keyword_default(
    value: int,
    *,
    memo: list[int] = [],  # noqa: B006
) -> dict[str, int]:
    memo.append(value)
    return {"out": len(memo)}


def _asks_for_its_globals(value: int) -> dict[str, int]:
    return {"out": value + globals().get("GLOBAL_OFFSET", -1)}


def _evaluates_a_global(value: int) -> dict[str, int]:
    return {"out": value + eval("GLOBAL_OFFSET")}


def _lists_its_scope(value: int) -> dict[str, int]:
    return {"out": value + len(dir())}


def _imports_a_module(value: int) -> dict[str, int]:
    import operator

    return {"out": operator.add(value, 1)}


def _reads_a_dunder(value: int) -> dict[str, int]:
    return {"out": value + len(value.__class__.__name__)}


def _names_a_dunder_in_a_format(value: int) -> dict[str, int]:
    return {"out": value + len(f"{value.__class__}")}


def _walks_a_frame(value: int) -> dict[str, int]:
    try:
        raise ValueError
    except ValueError as caught:
        frame = caught.__traceback__.tb_frame  # type: ignore[union-attr]
        return {"out": value + frame.f_globals["GLOBAL_OFFSET"]}


def _imports_in_a_nested_function(value: int) -> dict[str, int]:
    def inner() -> int:
        import operator

        return operator.add(value, 1)

    return {"out": inner()}


def _literal_defaults(
    value: int, offset: int = 1, *, label: str = "x", pair: tuple[int, ...] = (1, 2)
) -> dict[str, int]:
    return {"out": value + offset + len(label) - len(pair)}


def _uses_builtins_that_answer_from_their_arguments(value: int) -> dict[str, int]:
    try:
        if not isinstance(value, int):
            raise ValueError(value)
    except ValueError:
        return {"out": 0}
    return {"out": max(min(sum(sorted(range(3))), 9), abs(value) + 1, len(str(value)))}


def _lockstep_function(fn: Callable[..., Any]) -> tuple[PLC, PLC, dict]:
    def build() -> Program:
        with Program(strict=False) as program:
            with Rung():
                run_function(fn, ins={"value": N}, outs={"out": N})
        return program

    plc, twin = PLC(build(), dt=0.01), PLC(build(), dt=0.01)
    plc.run_for(3.0)
    truth: dict[int, Any] = {}
    _scan(twin, plc.state.scan_id, truth)
    fold_check._compare("compiled", plc.state.scan_id, plc.state, twin.state)
    _assert_history(plc, truth, [1, 50, 200, 250, plc.state.scan_id])
    return plc, twin, truth


@pytest.mark.parametrize(
    "fn",
    [
        _reads_a_global,
        _asks_for_its_globals,
        _evaluates_a_global,
        _lists_its_scope,
        _imports_a_module,
        _imports_in_a_nested_function,
        _reads_a_dunder,
        _names_a_dunder_in_a_format,
        _walks_a_frame,
        _closes_over()[0],
    ],
    ids=[
        "reads a global",
        "globals()",
        "eval",
        "dir",
        "import",
        "nested import",
        "dunder",
        "dunder in a format",
        "frame",
        "closure",
    ],
)
def test_a_function_that_could_keep_something_runs_itself(
    fn: Callable[..., Any], fine_loop: None
) -> None:
    """An undone batch calls it again, so a run steps it: these scans are the real ones."""
    plc, _twin, _truth = _lockstep_function(fn)
    assert plc._compiled_replay_supported_kernel() is None


@pytest.mark.parametrize(
    "fn",
    [
        _decorated,
        _default_names_a_global,
        _default_is_an_expression,
        lambda value: {"out": value + 1},
    ],
    ids=["decorated", "default", "default expression", "lambda"],
)
def test_a_function_no_copy_could_stand_for_runs_in_the_kernel(
    fn: Callable[..., Any], fine_loop: None
) -> None:
    """The kernel calls the function itself, so its source text and module don't matter."""
    plc, _twin, _truth = _lockstep_function(fn)
    assert plc._compiled_replay_supported_kernel() is not None


@pytest.mark.parametrize(
    "fn",
    [_keeps_state_in_a_default, _keeps_state_in_a_keyword_default],
    ids=["positional", "keyword-only"],
)
def test_a_mutable_default_is_the_function_s_own_list(
    fn: Callable[..., Any], fine_loop: None
) -> None:
    """An undone batch would count again, so a run steps it, and its list stays right."""

    def memo() -> list[int]:
        return fn.__defaults__[0] if fn.__defaults__ else fn.__kwdefaults__["memo"]

    def build() -> PLC:
        with Program(strict=False) as program:
            with Rung():
                calc(N + 1, N)
            with Rung(Lamp):
                run_function(fn, ins={"value": N}, outs={"out": Counted})
        plc = PLC(program, dt=0.01)
        plc.patch({Lamp: True})
        return plc

    memo().clear()
    plc = build()
    with fold_check.own_twin(plc) as compare:
        plc.run_until(Never, max_cycles=3000)
        counted = plc.state.tags[Counted.name]
        assert plc.state.scan_id == 3000
        assert counted == 3000
        assert len(memo()) == 3000

        memo().clear()
        twin = build()
        for _ in range(3000):
            twin.step()
        assert twin.state.tags[Counted.name] == counted
        compare(twin)
    assert plc._compiled_replay_supported_kernel() is None


def test_a_closure_is_called_on_every_scan(fine_loop: None) -> None:
    fn, calls = _closes_over()
    plc, _twin, _truth = _lockstep_function(fn)
    stepped = calls["count"]
    # Each runner scanned once, and each seek re-ran the scans it rebuilt.
    assert stepped >= 2 * plc.state.scan_id
    assert plc.state.tags["HsN"] == plc.state.scan_id


def test_a_global_store_lands_in_the_function_s_own_module(fine_loop: None) -> None:
    """On a kernel this would store into the kernel's module, silently."""
    global global_calls
    global_calls = 0
    plc, _twin, _truth = _lockstep_function(_stores_a_global)
    assert plc._compiled_replay_supported_kernel() is None
    assert global_calls >= 2 * plc.state.scan_id


def test_a_plant_function_that_could_keep_something_runs_itself(fine_loop: None) -> None:
    """The plant is part of the program a kernel would run."""
    fn, calls = _closes_over()

    def build() -> PLC:
        with Program(strict=False) as program:
            with Rung():
                out(Lamp)
        plc = PLC(program, dt=0.01)
        plc._synthesis = Synthesis(plant=[function_rung(fn, ins={"value": N}, outs={"out": N})])
        return plc

    plc, twin = build(), build()
    plc.run_for(3.0)
    _scan(twin, plc.state.scan_id, {})
    assert plc._compiled_replay_supported_kernel() is None
    assert calls["count"] >= 2 * plc.state.scan_id
    fold_check._compare("compiled", plc.state.scan_id, plc.state, twin.state)


@pytest.mark.parametrize(
    "fn",
    [
        _pure,
        _annotated_with_a_user_type,
        _literal_defaults,
        _uses_builtins_that_answer_from_their_arguments,
    ],
)
def test_a_function_that_means_the_same_keeps_the_kernel(
    fn: Callable[..., Any], fine_loop: None
) -> None:
    plc, _twin, _truth = _lockstep_function(fn)
    assert plc._compiled_replay_supported_kernel() is not None
