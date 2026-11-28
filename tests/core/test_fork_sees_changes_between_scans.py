"""A fork, how() or a replay at the last scan sees each change made since that scan.

Each test runs with the PC clock frozen at 02:00, 13:59 and 23:59.  The programs copy the RTC
hour and minute, so a run that ignores ``set_rtc()`` copies the PC clock and a run that reads
it copies 13:30.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung import PLC, Bool, Int, Program, Timer, copy, latch, on_delay, rung, system
from pyrung.core.analysis.graph import PlanStatus

SET = datetime(2026, 6, 15, 13, 30, 20)
SET_CLOCK = (13, 30)


@pytest.fixture(autouse=True, params=[(2, 0), (13, 59), (23, 59)], ids=["02:00", "13:59", "23:59"])
def pc_clock(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> tuple[int, int]:
    hour, minute = request.param

    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return datetime(2026, 11, 28, hour, minute, 0)

    monkeypatch.setattr("pyrung.core.runner.datetime", _Frozen)
    monkeypatch.setattr("pyrung.core.compiled_plc.datetime", _Frozen)
    return hour, minute


def _clock_copier(scans_before: int) -> tuple[PLC, Bool]:
    """Return a PLC that copies the RTC hour and minute each scan, stepped *scans_before* times."""
    go = Bool("Go", external=True)
    hour, minute, hit = Int("H"), Int("M"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung():
            copy(system.rtc.hour, hour)
            copy(system.rtc.minute, minute)
        with rung(go, hour >= 13):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    for _ in range(scans_before):
        plc.step()
    return plc, hit


def _clock(plc: PLC) -> tuple[int, int]:
    tags = plc.current_state.tags
    return tags["H"], tags["M"]


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_at_the_last_scan_runs_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)

    fork = plc.fork()
    fork.step()
    plc.step()

    assert _clock(fork) == SET_CLOCK
    assert _clock(plc) == SET_CLOCK


@pytest.mark.parametrize("scans_before", [0, 1])
def test_how_at_the_last_scan_plans_on_the_set_clock(scans_before: int) -> None:
    plc, hit = _clock_copier(scans_before)
    plc.set_rtc(SET)

    plan = plc.how(hit, max_scans=200)

    assert plan.status is PlanStatus.REACHED
    assert _clock(plan.fork) == SET_CLOCK
    assert plan.replay().state.tags == plan.fork.state.tags


@pytest.mark.parametrize("scans_before", [0, 1, 2])
def test_a_plan_that_how_trims_runs_on_the_set_clock(scans_before: int) -> None:
    """how() runs a plan again from its first scan when it drops temporary logic."""
    enable, hit = Bool("En", external=True), Bool("Hit")
    hour = Int("H")
    wait = Timer.clone("W")
    with Program(strict=False) as prog:
        with rung():
            copy(system.rtc.hour, hour)
        with rung(enable):
            on_delay(wait, 3000)
        with rung(wait.Done):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    for _ in range(scans_before):
        plc.step()
    plc.set_rtc(SET)

    plan = plc.how(hit, max_scans=500)

    assert plan.status is PlanStatus.REACHED
    assert plan.fork.state.tags["H"] == 13
    assert plan.replay().state.tags == plan.fork.state.tags


@pytest.mark.parametrize("scans_before", [0, 1])
def test_replay_to_the_last_scan_steps_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)

    replay = plc.replay_to(scans_before)
    replay.step()

    assert _clock(replay) == SET_CLOCK


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_seek_in_the_fork_replays_its_first_scan_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)
    fork = plc.fork()
    fork.run(5)

    assert _clock(fork.replay_to(scans_before + 1)) == SET_CLOCK
    assert fork.seek(scans_before + 1).tags["H"] == 13


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_seek_in_the_parent_replays_the_next_scan_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)
    plc.run(5)

    assert _clock(plc.replay_to(scans_before + 1)) == SET_CLOCK
    assert plc.seek(scans_before + 1).tags["H"] == 13


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_of_a_fork_runs_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)
    fork = plc.fork()

    at_its_tip = fork.fork()
    at_its_tip.step()
    fork.run(3)
    at_its_first_scan = fork.fork(scans_before)
    at_its_first_scan.step()

    assert _clock(at_its_tip) == SET_CLOCK
    assert _clock(at_its_first_scan) == SET_CLOCK


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_without_the_parent_log_runs_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)

    fork = plc.fork(inherit_log=False)
    fork.run(3)
    at_its_first_scan = fork.fork(scans_before)
    at_its_first_scan.step()

    assert _clock(fork) == SET_CLOCK
    assert _clock(fork.replay_to(scans_before + 1)) == SET_CLOCK
    assert _clock(at_its_first_scan) == SET_CLOCK


@pytest.mark.parametrize("scans_before", [0, 1])
def test_the_last_of_two_set_rtc_calls_reaches_the_fork(scans_before: int) -> None:
    plc, hit = _clock_copier(scans_before)
    plc.set_rtc(datetime(2026, 6, 15, 7, 0, 0))
    plc.set_rtc(SET)

    fork = plc.fork()
    fork.step()
    replay = plc.replay_to(scans_before)
    replay.step()
    plan = plc.how(hit, max_scans=200)

    assert _clock(fork) == SET_CLOCK
    assert _clock(replay) == SET_CLOCK
    assert _clock(plan.fork) == SET_CLOCK


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_after_stop_and_set_rtc_restarts_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.set_rtc(SET)
    plc.stop()

    fork = plc.fork()
    fork.step()
    fork.step()
    plc.step()
    plc.step()

    assert (_clock(fork), fork.mode) == (_clock(plc), plc.mode)
    assert _clock(fork) == SET_CLOCK


def test_a_fork_of_an_earlier_scan_leaves_a_later_set_to_the_next_scan(
    pc_clock: tuple[int, int],
) -> None:
    """A fork of an earlier scan starts on the clock that scan ran on.

    Forces and patches made after that scan also stay out of the fork.
    """
    plc, _hit = _clock_copier(1)
    plc.set_rtc(SET)
    plc.run(3)

    fork = plc.fork(1)
    fork.step()

    assert _clock(fork) == pc_clock


def test_the_parent_and_the_fork_keep_their_own_clocks() -> None:
    plc, _hit = _clock_copier(1)
    plc.set_rtc(SET)
    fork = plc.fork()

    plc.set_rtc(datetime(2026, 6, 15, 7, 0, 0))
    fork.step()
    fork.set_rtc(datetime(2026, 6, 15, 21, 0, 0))
    plc.step()

    assert _clock(fork) == SET_CLOCK
    assert _clock(plc) == (7, 0)


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_at_the_last_scan_is_stopped_after_stop(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.stop()

    fork = plc.fork()

    assert fork.mode == "stop"


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_at_the_last_scan_has_the_battery_setting(scans_before: int) -> None:
    plc, _hit = _clock_copier(scans_before)
    plc.battery_present = False

    fork = plc.fork()

    assert fork.battery_present is False
