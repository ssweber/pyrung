"""A fork, how() or a replay at the last scan sees each change made since that scan.

The PC clock is frozen at 02:00 in every test, so a run that ignores ``set_rtc()`` copies
hour 2 and a run that reads it copies hour 13.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from pyrung import PLC, Bool, Int, Program, copy, latch, rung, system
from pyrung.core.analysis.graph import PlanStatus

SET = datetime(2026, 6, 15, 13, 30, 20)


@pytest.fixture(autouse=True)
def _pc_clock_at_two(monkeypatch: pytest.MonkeyPatch) -> None:
    class _Frozen(datetime):
        @classmethod
        def now(cls, tz=None):  # noqa: ARG003
            return datetime(2026, 11, 28, 2, 0, 0)

    monkeypatch.setattr("pyrung.core.runner.datetime", _Frozen)


def _hour_copier(scans_before: int) -> tuple[PLC, Bool]:
    """Return a PLC that copies the RTC hour each scan, stepped *scans_before* times."""
    go = Bool("Go", external=True)
    copied, hit = Int("Copied"), Bool("Hit")
    with Program(strict=False) as prog:
        with rung():
            copy(system.rtc.hour, copied)
        with rung(go, copied >= 13):
            latch(hit)
    plc = PLC(prog, dt=0.01)
    for _ in range(scans_before):
        plc.step()
    return plc, hit


def _copied(plc: PLC) -> int:
    return plc.current_state.tags["Copied"]


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_at_the_last_scan_runs_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _hour_copier(scans_before)
    plc.set_rtc(SET)

    fork = plc.fork()
    fork.step()
    plc.step()

    assert _copied(fork) == 13
    assert _copied(plc) == 13


@pytest.mark.parametrize("scans_before", [0, 1])
def test_how_at_the_last_scan_plans_on_the_set_clock(scans_before: int) -> None:
    plc, hit = _hour_copier(scans_before)
    plc.set_rtc(SET)

    plan = plc.how(hit, max_scans=200)

    assert plan.status is PlanStatus.REACHED
    assert plan.fork.state.tags["Copied"] == 13
    assert plan.replay().state.tags == plan.fork.state.tags


@pytest.mark.parametrize("scans_before", [0, 1])
def test_replay_to_the_last_scan_steps_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _hour_copier(scans_before)
    plc.set_rtc(SET)

    replay = plc.replay_to(scans_before)
    replay.step()

    assert _copied(replay) == 13


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_seek_in_the_fork_replays_its_first_scan_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _hour_copier(scans_before)
    plc.set_rtc(SET)
    fork = plc.fork()
    fork.run(5)

    assert fork.replay_to(scans_before + 1).current_state.tags["Copied"] == 13
    assert fork.seek(scans_before + 1).tags["Copied"] == 13


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_seek_in_the_parent_replays_the_next_scan_on_the_set_clock(scans_before: int) -> None:
    plc, _hit = _hour_copier(scans_before)
    plc.set_rtc(SET)
    plc.run(5)

    assert plc.replay_to(scans_before + 1).current_state.tags["Copied"] == 13
    assert plc.seek(scans_before + 1).tags["Copied"] == 13


def test_a_fork_of_an_earlier_scan_leaves_a_later_set_to_the_next_scan() -> None:
    """A set_rtc() after an earlier scan belongs to the next scan, as a force or a patch does."""
    plc, _hit = _hour_copier(1)
    plc.set_rtc(SET)
    plc.run(3)

    fork = plc.fork(1)
    fork.step()

    assert _copied(fork) == 2


def test_the_parent_and_the_fork_keep_their_own_clocks() -> None:
    plc, _hit = _hour_copier(1)
    plc.set_rtc(SET)
    fork = plc.fork()

    plc.set_rtc(datetime(2026, 6, 15, 7, 0, 0))
    fork.step()
    fork.set_rtc(datetime(2026, 6, 15, 21, 0, 0))
    plc.step()

    assert _copied(fork) == 13
    assert _copied(plc) == 7


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_at_the_last_scan_is_stopped_after_stop(scans_before: int) -> None:
    plc, _hit = _hour_copier(scans_before)
    plc.stop()

    fork = plc.fork()

    assert fork.mode == "stop"


@pytest.mark.parametrize("scans_before", [0, 1])
def test_a_fork_at_the_last_scan_has_the_battery_setting(scans_before: int) -> None:
    plc, _hit = _hour_copier(scans_before)
    plc.battery_present = False

    fork = plc.fork()

    assert fork.battery_present is False
