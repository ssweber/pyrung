"""how() and a program that stops itself.

A rung that sets ``sys.cmd_mode_stop`` ends the recording: the next scan is a restart
with scan ids from 0.  A plan is the scans of one recording, so how() says it stopped
rather than step over it, or plan a route stepping would never run.
"""

from __future__ import annotations

import pytest

from pyrung import PLC, Bool, Int, Program, Rung, Timer, latch, math, on_delay, out
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.system_points import system

StopTimer = Timer.clone("StopTimer")
LampTimer = Timer.clone("LampTimer")
Lamp = Bool("Lamp")
Start, Arm, Done, Tick, Count, Kill = (
    Bool("Start"),
    Bool("Arm"),
    Bool("Done"),
    Bool("Tick"),
    Int("Count"),
    Bool("Kill"),
)


@pytest.mark.parametrize(
    "stop_at, lamp_at",
    [(100, 300), (20_000, 30_000)],
    ids=["interpreted", "at the end of a stretch"],
)
def test_a_stop_before_the_target_is_the_answer(stop_at: int, lamp_at: int) -> None:
    with Program() as logic:
        with Rung():
            on_delay(StopTimer, preset=stop_at)
        with Rung(StopTimer.Done):
            out(system.sys.cmd_mode_stop)
        with Rung():
            on_delay(LampTimer, preset=lamp_at)
        with Rung(LampTimer.Done):
            out(Lamp)
    plc = PLC(logic, dt=0.001)

    # Stepped: the program stops one scan after the timer is done, and starts again from
    # nothing, so the lamp never comes on.
    stepped = plc.fork()
    while stepped._running:
        stepped.step()
    assert stepped.state.scan_id == stop_at + 1
    for _ in range(lamp_at // 100):
        stepped.step()
    assert stepped.state.scan_id == lamp_at // 100

    plan = plc.how(Lamp, max_scans=4000)
    assert plan.status is PlanStatus.STOPPED
    assert plan.reason == (
        f"the program stopped itself at scan {stop_at + 1}. Its next scan would restart it, "
        "and how() does not plan across a restart."
    )


def test_the_returned_fork_is_not_a_search() -> None:
    # The stop is after the target, so the search never meets it and the caller does.
    with Program() as logic:
        with Rung():
            on_delay(LampTimer, preset=50)
        with Rung(LampTimer.Done):
            out(Lamp)
        with Rung(Lamp):
            on_delay(StopTimer, preset=5)
        with Rung(StopTimer.Done):
            out(system.sys.cmd_mode_stop)
    plc = PLC(logic, dt=0.001)
    plan = plc.how(Lamp, max_scans=4000)
    assert plan.status is PlanStatus.REACHED
    fork = plan.fork
    assert fork._coast_memo is None
    assert fork._search_refuses_restart is False
    assert fork.fork()._search_refuses_restart is False

    stepped = plc.fork()
    for _ in range(fork.state.scan_id):
        stepped.step()
    assert stepped.state.tags == fork.state.tags
    for _ in range(40):
        fork.step()
        stepped.step()
        assert stepped.state.scan_id == fork.state.scan_id
        assert stepped.state.tags == fork.state.tags
    assert fork.state.scan_id < 40, "the restart happened, as stepping restarts"


def _stop_by_calling_stop(plc: PLC) -> None:
    plc.stop()


def _stop_from_the_program(plc: PLC) -> None:
    # The PLC reads the stop bit one scan after the program writes it.
    plc.patch({"Kill": True})
    plc.step()
    plc.step()


def _patch_the_count(plc: PLC) -> None:
    plc.patch({"Count": 3})


def _force_the_count(plc: PLC) -> None:
    plc.force("Count", 45)


def _keep_a_forced_input_on_run(plc: PLC) -> None:
    plc.keep_forces_on_run = True
    plc.force("Start", True)


def _wait_for_nothing(plc: PLC) -> None:
    pass


def _counting_program() -> Program:
    with Program(strict=False) as logic:
        with Rung(~Tick):
            out(Tick)
        with Rung(Tick):
            math(Count + 1, Count)
        with Rung(Start, Arm, Count > 40):
            latch(Done)
        with Rung(Kill):
            out(system.sys.cmd_mode_stop)
    return logic


@pytest.mark.parametrize(
    "stop, then",
    [
        (_stop_by_calling_stop, _wait_for_nothing),
        (_stop_by_calling_stop, _patch_the_count),
        (_stop_by_calling_stop, _force_the_count),
        (_stop_by_calling_stop, _keep_a_forced_input_on_run),
        (_stop_from_the_program, _patch_the_count),
        (_stop_from_the_program, _keep_a_forced_input_on_run),
    ],
    ids=[
        "stop",
        "stop, patch",
        "stop, force",
        "stop, forced input kept on run",
        "program stop, patch",
        "program stop, forced input kept on run",
    ],
)
def test_how_on_a_plc_in_stop_says_stopped(stop, then) -> None:
    plc = PLC(_counting_program(), dt=0.01)
    for _ in range(50):
        plc.step()
    stop(plc)
    then(plc)
    assert plc.mode == "stop"
    scan_id = plc.state.scan_id
    state = plc.state

    plan = plc.how(Done, max_scans=500)

    assert plan.status is PlanStatus.STOPPED
    assert plan.fork is None
    assert plan.anchor_scan == scan_id
    assert plan.reason == (
        f"the PLC is in STOP at scan {scan_id}. Its next scan would restart it, "
        "and how() does not plan across a restart."
    )
    assert str(plan).startswith("Stopped before reaching Done=True.")
    assert plc.state is state


@pytest.mark.parametrize("then", [_patch_the_count, _force_the_count], ids=["patch", "force"])
def test_how_says_stopped_when_the_waiting_scan_stops_the_plc(then) -> None:
    # The program writes the stop bit on scan 51, so the scan that applies the patch or force
    # is the one that stops.
    plc = PLC(_counting_program(), dt=0.01)
    for _ in range(50):
        plc.step()
    plc.patch({"Kill": True})
    plc.step()
    then(plc)
    assert plc.mode == "run"

    plan = plc.how(Done, max_scans=500)

    assert plan.status is PlanStatus.STOPPED
    assert plan.last_resort is None
    assert plan.reason == (
        "the program stopped itself at scan 52. Its next scan would restart it, "
        "and how() does not plan across a restart."
    )
