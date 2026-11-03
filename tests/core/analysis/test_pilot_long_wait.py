"""how() behind a long wait: the wait is not what ends it, and it lands where stepping does.

A seek under the plan's own holds pays for its scans by what they cost, a loop scan being a
hundredth of an interpreted one, so the length of a timer does not turn a right answer into a
decline.  Scan counts here are the timers' own arithmetic, checked by stepping the recording
where that is cheap enough to step.
"""

from __future__ import annotations

from typing import Any

import pytest

from pyrung import Bool, Dint, Int, Program, Rung, Timer, copy, latch, on_delay, out, time_drum
from pyrung.core import PLC
from pyrung.core.analysis.pilot.budget import STRETCH_SCANS_PER_UNIT
from pyrung.core.analysis.pilot.coast import CoastSession
from pyrung.core.coast_run import coast_until, work_charged


def _chain(first: int, second: int, unit: str) -> tuple[Program, Bool]:
    go = Bool("LwGo")
    lamp = Bool("LwLamp")
    t1 = Timer.clone("LwT1")
    t2 = Timer.clone("LwT2")
    with Program(strict=False) as prog:
        with Rung(go):
            on_delay(t1, preset=first, unit=unit)
        with Rung(t1.Done):
            on_delay(t2, preset=second, unit=unit)
        with Rung(t2.Done):
            latch(lamp)
            copy(5, Int("LwStage"))
    return prog, lamp


def _one_timer(preset: int, unit: str, coil: Any = latch) -> tuple[Program, Bool]:
    go = Bool("LwGo")
    lamp = Bool("LwLamp")
    t1 = Timer.clone("LwT1")
    with Program(strict=False) as prog:
        with Rung(go):
            on_delay(t1, preset=preset, unit=unit)
        with Rung(t1.Done):
            coil(lamp)
    return prog, lamp


def _soak() -> tuple[Program, Bool]:
    start = Bool("LwStart", external=True)
    run = Bool("LwRun")
    soak = Timer.clone("LwSoak")
    done = Bool("LwDone")
    with Program(strict=False) as prog:
        with Rung(start):
            latch(run)
        with Rung(run):
            on_delay(soak, 120, "s")
        with Rung(soak.Done):
            out(done)
    return prog, done


def _soak_beside_an_oscillator() -> tuple[Program, Bool]:
    osc = Bool("LwOsc")
    start = Bool("LwStart", external=True)
    run = Bool("LwRun")
    soak = Timer.clone("LwSoak")
    watchdog = Timer.clone("LwWatchdog")
    done = Bool("LwDone")
    with Program(strict=False) as prog:
        with Rung(start):
            latch(run)
        with Rung(run):
            on_delay(soak, 60, "s")
        with Rung():
            on_delay(watchdog, 200, "ms").reset(osc)
        with Rung(soak.Done):
            out(done)
        with Rung(~osc):
            out(osc)
    return prog, done


def _drum() -> tuple[Program, Bool]:
    done = Bool("LwDrumDone")
    start = Bool("LwStart", external=True)
    with Program(strict=False) as prog:
        with Rung(start):
            time_drum(
                outputs=[Bool("LwO1"), Bool("LwO2")],
                presets=[20, 40],
                unit="s",
                pattern=[[1, 0], [0, 1]],
                current_step=Int("LwStep"),
                accumulator=Dint("LwAcc"),
                completion_flag=done,
            ).reset(Bool("LwReset", external=True))
    return prog, done


def _how(prog: Program, goal: Bool) -> Any:
    plc = PLC(prog, dt=0.01)
    plc.step()
    return plc.how(goal, wall_clock_s=60.0)


def _stepped_from_the_start(fork: PLC) -> PLC:
    """The recording, interpreted scan by scan from scan 0, past every checkpoint it has."""
    replay, log, anchor, lifecycle = fork._build_replay_fork(None)
    for scan in range(anchor + 1, fork.state.scan_id + 1):
        fork._apply_log_entries_for_scan(replay, scan, log, lifecycle)
        replay.step()
    return replay


@pytest.mark.parametrize(
    ("first", "second", "unit", "scans"),
    [
        (3, 2, "min", 30_001),
        (6, 4, "min", 60_001),
        (12, 8, "min", 120_001),
        (20, 10, "min", 180_001),
        (1200, 1, "sec", 120_099),
        (2, 2, "h", 1_439_999),
    ],
)
def test_a_chain_of_timers_under_a_hold_lands_on_the_first_scan(
    first: int, second: int, unit: str, scans: int
) -> None:
    prog, goal = _chain(first, second, unit)
    path = _how(prog, goal)
    assert path.reachable, str(path)
    fork = path.fork
    assert fork.state.scan_id == 1 + scans
    assert fork.state.tags[goal.name] is True
    assert fork.history.at(fork.state.scan_id - 1).tags[goal.name] is False
    assert path.replay().state.tags == fork.state.tags


@pytest.mark.parametrize(
    ("preset", "unit", "coil", "scans"),
    [
        pytest.param(9990, "sec", latch, 999_000, id="9990s"),
        pytest.param(9995, "sec", latch, 999_500, id="9995s"),
        pytest.param(10_000, "sec", latch, 1_000_000, id="10000s"),
        pytest.param(3, "h", latch, 1_080_000, id="3h"),
        pytest.param(4, "h", latch, 1_440_000, id="4h"),
        pytest.param(4, "h", out, 1_440_000, id="4h_out"),
        pytest.param(8, "h", latch, 2_880_000, id="8h"),
        pytest.param(24, "h", latch, 8_640_000, id="24h"),
    ],
)
def test_one_timer_past_a_million_scans_lands_on_the_first_scan(
    preset: int, unit: str, coil: Any, scans: int
) -> None:
    """The estimate pays for the loop's scans, and the interpreted scans around them are paid too."""
    prog, goal = _one_timer(preset, unit, coil)
    path = _how(prog, goal)
    assert path.reachable, str(path)
    fork = path.fork
    assert fork.state.scan_id == 1 + scans
    assert fork.state.tags[goal.name] is True
    assert fork.state.tags["LwT1_Done"] is True
    assert fork.history.at(fork.state.scan_id - 1).tags[goal.name] is False
    assert path.replay().state.tags == fork.state.tags


@pytest.mark.parametrize("coil", [latch, out], ids=["latch", "out"])
def test_one_timer_stepped_from_the_start_lands_there_too(coil: Any) -> None:
    prog, goal = _one_timer(5, "min", coil)
    path = _how(prog, goal)
    assert path.reachable, str(path)
    fork = path.fork
    assert fork.state.scan_id == 30_002
    stepped = _stepped_from_the_start(fork)
    assert stepped.state.scan_id == fork.state.scan_id
    assert stepped.state.tags == fork.state.tags
    assert stepped.state.tags[goal.name] is True
    assert stepped.history.at(fork.state.scan_id - 1).tags[goal.name] is False


@pytest.mark.parametrize(
    ("build", "landing"),
    [
        pytest.param(lambda: _chain(3, 2, "min"), 30_002, id="chain"),
        pytest.param(_soak, 12_001, id="soak"),
        pytest.param(_soak_beside_an_oscillator, 6_001, id="osc_soak"),
        pytest.param(_drum, 6_002, id="drum"),
    ],
)
def test_the_recording_stepped_from_the_start_lands_there_too(build: Any, landing: int) -> None:
    prog, goal = build()
    path = _how(prog, goal)
    assert path.reachable, str(path)
    fork = path.fork
    assert fork.state.scan_id == landing
    stepped = _stepped_from_the_start(fork)
    assert stepped.state.scan_id == landing
    assert stepped.state.tags == fork.state.tags
    assert stepped.state.tags[goal.name] is True
    assert stepped.history.at(landing - 1).tags[goal.name] is False


def test_a_drum_reset_on_the_way_in_leaves_the_drum_to_run_its_whole_time() -> None:
    """The plan's one scan of reset takes the step from 0 to 1, and the drum then runs from there."""
    prog, goal = _drum()
    path = _how(prog, goal)
    assert path.reachable, str(path)
    fork = path.fork
    assert fork.history.at(2).tags["LwReset"] is True
    assert fork.history.at(2).tags["LwStep"] == 1
    assert fork.history.at(3).tags["LwReset"] is False
    assert fork.state.tags["LwStep"] == 2


def test_a_small_ledger_is_not_spent_on_one_wait() -> None:
    """A search asked to stay small counts its wait in scans, and so charges a hundredth of it."""
    prog, goal = _chain(3, 2, "min")
    plc = PLC(prog, dt=0.01)
    plc.step()
    path = plc.how(goal, wall_clock_s=60.0, max_scans=400)
    assert not path.reachable
    assert "budget exhausted" not in str(path)


def test_a_goal_no_rung_can_reach_declines_at_once() -> None:
    go = Bool("LwGo")
    lamp = Bool("LwLamp")
    block = Bool("LwBlock")
    t1 = Timer.clone("LwT1")
    t2 = Timer.clone("LwT2")
    with Program(strict=False) as prog:
        with Rung(go, ~t2.Done):
            on_delay(t1, preset=30, unit="sec")
        with Rung(t1.Done, ~t2.Done):
            on_delay(t2, preset=30, unit="sec")
        with Rung(t1.Done, t2.Done):
            latch(block)
        with Rung(block, ~block):
            latch(lamp)
    path = _how(prog, lamp)
    assert not path.reachable
    assert path.fork is None


def test_a_wait_under_a_hold_that_never_ends_stops_at_the_ledger() -> None:
    """A timer that resets itself never gets to the goal's value, and its loop could run for ever."""
    go = Bool("LwGo")
    lamp = Bool("LwLamp")
    t1 = Timer.clone("LwT1")
    with Program(strict=False) as prog:
        with Rung(go, ~t1.Done):
            on_delay(t1, preset=10, unit="sec")
        with Rung(t1.Acc > 20000):
            latch(lamp)
    path = _how(prog, lamp)
    assert not path.reachable
    assert "budget exhausted" in str(path)


def _timer_program() -> Program:
    with Program(strict=False) as prog:
        with Rung():
            on_delay(Timer.clone("LwPlain"), 3_000, "s")
    return prog


def test_a_budget_in_work_lands_where_stepping_does() -> None:
    """Fifty units of budget at a hundred loop scans each, and the state stepping leaves there."""
    plc = PLC(_timer_program(), dt=0.01)
    plc.step()
    stats: dict[str, int] = {}
    coast_until(
        plc,
        lambda _state: False,
        budget=50,
        predicate_reads=frozenset(),
        stats=stats,
        loop_scans_per_unit=STRETCH_SCANS_PER_UNIT,
    )
    logical = stats["logical_scans"]
    assert logical > 50
    assert stats["stretched_scans"] > 0
    # The stats' kernel scans have the landing too, which the loop may have run.
    assert work_charged(logical, stats["kernel_scans"], STRETCH_SCANS_PER_UNIT) in (50, 51)
    stepped = PLC(_timer_program(), dt=0.01)
    for _ in range(1 + logical):
        stepped.step()
    assert plc.state.scan_id == stepped.state.scan_id
    assert plc.state.tags == stepped.state.tags


def test_only_a_live_wait_under_a_hold_counts_work() -> None:
    prog, goal = _chain(3, 2, "min")
    path = _how(prog, goal)
    assert path.reachable, str(path)
    fork = path.fork
    assert fork._synthesis.holds
    assert CoastSession(fork, waits_on_work=True).loop_scans_per_unit() == STRETCH_SCANS_PER_UNIT
    assert CoastSession(fork).loop_scans_per_unit() == 1
    unheld = PLC(_timer_program(), dt=0.01)
    assert CoastSession(unheld, waits_on_work=True).loop_scans_per_unit() == 1
