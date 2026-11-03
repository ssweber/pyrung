"""What a how() may spend: the ledger in work units and the clock behind it."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pyrung import PLC, Bool, Physical, Program, Rung, Timer, on_delay, out
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import budget
from pyrung.core.analysis.pilot.budget import SearchBudget
from pyrung.core.analysis.pilot.working_theory import TheoryTermination

Slow = Timer.clone("Slow")
Lamp = Bool("Lamp")
y_Done = Bool("y_Done")


def _receipt(*scans: int, epoch: str = "e") -> SimpleNamespace:
    return SimpleNamespace(
        spans=(SimpleNamespace(epoch=SimpleNamespace(reference=epoch), kernel_scan_ids=scans),)
    )


def _slow_lamp(preset: int) -> PLC:
    with Program() as logic:
        with Rung():
            on_delay(Slow, preset=preset)
        with Rung(Slow.Done):
            out(Lamp)
    return PLC(logic, dt=0.001)


class TestTheLedger:
    def test_an_interpreted_scan_is_a_unit(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(1, 2, 3))
        assert ledger.spent == 3
        assert ledger.scans_run == 3

    def test_a_scan_is_charged_once_across_consumers(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(1, 2, 3))
        ledger.charge_execution(_receipt(1, 2, 3))
        assert ledger.spent == 3

    def test_a_hundred_loop_scans_are_a_unit(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(1, 1002))
        assert ledger.scans_run == 1002
        assert ledger.spent == 2 + 10

    def test_a_stretch_is_charged_once_and_its_remainder_is_kept(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(1, 52))
        assert ledger.spent == 2
        ledger.charge_execution(_receipt(1, 52))
        assert ledger.spent == 2
        ledger.charge_execution(_receipt(52, 103))
        assert ledger.spent == 3 + 1

    def test_a_how_cannot_run_free_in_the_loop(self) -> None:
        # Its 2,000,000 loop scans would be 20,000 units.  The seek's own budget is logical,
        # so what ends it is a search that has spent its work.
        plan = _slow_lamp(2_000_000).how(Lamp, max_scans=200)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("budget exhausted")


def _linked_motor() -> PLC:
    x_Go = Bool("x_Go")
    o_Motor = Bool("o_Motor")
    x_MotorFB = Bool("x_MotorFB", physical=Physical("MotorFb", on_delay="20ms"), link="o_Motor")
    with Program() as logic:
        with Rung(x_Go):
            out(o_Motor)
        with Rung(o_Motor, x_MotorFB):
            out(y_Done)
    return PLC(logic, dt=0.01)


class TestAStopIsNotAVerdict:
    # A physical link that the search has not got past yet is not one it cannot get past.

    def test_the_program_reaches_its_target(self) -> None:
        plan = _linked_motor().how(y_Done, wall_clock_s=None)
        assert plan.status is PlanStatus.REACHED

    def test_out_of_time_says_so_and_nothing_about_a_link(self, monkeypatch) -> None:
        monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)
        plan = _linked_motor().how(y_Done, wall_clock_s=0.0)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("wall-clock budget exhausted")
        assert "physical link" not in plan.reason

    def test_out_of_work_says_so_and_nothing_about_a_link(self) -> None:
        plan = _linked_motor().how(y_Done, max_scans=0, wall_clock_s=None)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("budget exhausted")
        assert "physical link" not in plan.reason


class TestTheClock:
    @pytest.fixture(autouse=True)
    def _clock_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)

    def test_out_of_time_is_its_own_terminal(self) -> None:
        plan = _slow_lamp(30_000).how(Lamp, wall_clock_s=0.0)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("wall-clock budget exhausted (0 s, ")
        assert " scans run)" in plan.reason
        assert plan.reason.isascii()

    def test_none_turns_it_off(self) -> None:
        plan = _slow_lamp(300).how(Lamp, wall_clock_s=None)
        assert plan.reachable

    def test_time_enough_changes_nothing(self) -> None:
        timed = _slow_lamp(300).how(Lamp, wall_clock_s=3600.0)
        free = _slow_lamp(300).how(Lamp, wall_clock_s=None)
        assert timed.reachable and free.reachable
        assert str(timed) == str(free)

    def test_the_default_is_on_and_a_test_turns_it_off(self, monkeypatch) -> None:
        assert budget.WallClock.starting_now(budget.DEFAULT_WALL_CLOCK_S) is not None
        monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", False)
        assert budget.WallClock.starting_now(budget.DEFAULT_WALL_CLOCK_S) is None

    def test_a_loop_stretch_stops_for_the_clock(self, monkeypatch) -> None:
        # The clock runs out once the search is in the loop, with scans behind it.
        from pyrung.core import coast_run

        real = coast_run._try_stretch
        clock_ran_out = {"now": False}

        def stretch(*args, **kwargs):
            clock_ran_out["now"] = True
            return real(*args, **kwargs)

        monkeypatch.setattr(coast_run, "_try_stretch", stretch)

        def check(self) -> None:
            if clock_ran_out["now"]:
                raise budget.SearchOutOfTime

        monkeypatch.setattr(budget.WallClock, "check", check)
        plan = _slow_lamp(30_000).how(Lamp, wall_clock_s=1.0)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("wall-clock budget exhausted (1 s, ")

    def test_a_theory_can_end_by_the_clock(self) -> None:
        assert TheoryTermination.WALL_CLOCK.value == "wall-clock"
