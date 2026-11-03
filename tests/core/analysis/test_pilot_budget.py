"""What a how() may spend: the ledger in work units and the clock behind it."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import PLC, Bool, Int, Physical, Program, Rung, Timer, on_delay, out, run_function
from pyrung.core import coast_run, runner
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import budget
from pyrung.core.analysis.pilot import coast as pilot_coast
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot.budget import SearchBudget
from pyrung.core.analysis.pilot.causal import _shared_cause, action_caused_change
from pyrung.core.analysis.pilot.working_theory import TheoryTermination
from pyrung.core.rung_firings import SearchQuestionRefused, UnrecordedScansError

Slow = Timer.clone("Slow")
Lamp = Bool("Lamp")
y_Done = Bool("y_Done")
Seen = Int("Seen")


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

    def test_a_scan_is_charged_once_across_consumers(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(1, 2, 3))
        ledger.charge_execution(_receipt(1, 2, 3))
        assert ledger.spent == 3

    def test_a_hundred_loop_scans_are_a_unit(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(1, 1002))
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


class _Stateful:
    """A function that keeps state between calls, so no kernel may run its program.

    What it says doesn't depend on that state, or a lockstep twin sharing it would differ.
    """

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> dict[str, int]:
        self.calls += 1
        return {"seen": 3}


@pytest.fixture
def most_calls(request: pytest.FixtureRequest) -> int:
    """What a search over 3000 scans may call the function, a scan being one call.

    Under lockstep every coast's twin calls it too, and so do the windows stepped again to
    compare (21002 calls, measured), still far under a seek that ignores the ledger (120000).
    """
    return 3200 * (10 if request.config.getoption("fold_check", default=False) else 1)


def _no_loop(preset_scans: int) -> tuple[PLC, _Stateful]:
    function = _Stateful()
    with Program() as logic:
        with Rung():
            on_delay(Slow, preset=preset_scans)
        with Rung():
            run_function(function, outs={"seen": Seen})
        with Rung(Slow.Done):
            out(Lamp)
    plc = PLC(logic, dt=0.001)
    assert plc._compiled_replay_supported_kernel() is None
    return plc, function


class TestASearchWithNoLoop:
    # Every scan is interpreted, and max_scans is how many of them it may run.

    def test_a_long_timer_stops_at_the_ledger(self, most_calls: int) -> None:
        plc, function = _no_loop(120_000)
        plan = plc.how(Lamp, max_scans=3000, wall_clock_s=None)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("budget exhausted")
        assert function.calls < most_calls

    def test_it_stops_at_what_it_may_hold(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # The bound on memory, for when max_scans is no bound at all.
        monkeypatch.setattr(budget, "MAX_SEARCH_BLOCKS", 1_000)
        monkeypatch.setattr(budget, "_POLLS_PER_BLOCK_COUNT", 1)
        plc, _function = _no_loop(1_000_000)
        plan = plc.how(Lamp, max_scans=10**9, wall_clock_s=None)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("memory budget exhausted")
        assert "physical link" not in plan.reason

    def test_the_ledger_does_not_stop_what_it_can_pay_for(self, most_calls: int) -> None:
        plc, function = _no_loop(1000)
        plan = plc.how(Lamp, max_scans=3000, wall_clock_s=None)
        assert plan.status is PlanStatus.REACHED
        assert plan.fork.state.scan_id == 1000
        assert function.calls < most_calls


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


class TestARefusedQuestion:
    # Scans the search cannot afford to read are not evidence that nothing happened in them.

    @pytest.fixture
    def unrecorded(self, monkeypatch: pytest.MonkeyPatch) -> PLC:
        """A fork in a search, whose scans have no records and may not get them."""
        monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", 0)
        monkeypatch.setattr(runner, "SETTLE_SCAN_LIMIT", 0)
        plc = _slow_lamp(100_000).fork()
        coast_run.coast_until(plc, lambda _state: False, budget=5000)
        assert plc._fold_windows
        plc._search_refuses_restart = True
        return plc

    def test_action_caused_change_does_not_call_it_no_cause(self, unrecorded: PLC) -> None:
        tip = unrecorded.state.scan_id
        with pytest.raises(SearchQuestionRefused):
            action_caused_change(
                unrecorded, "Go", Slow.Acc.name, frozenset({"Go"}), scan=tip, start_scan=tip - 3
            )

    def test_shared_cause_does_not_call_it_no_cause(self, unrecorded: PLC) -> None:
        with pytest.raises(SearchQuestionRefused):
            _shared_cause(unrecorded, Slow.Acc.name)

    def test_outside_a_search_it_is_the_public_error(self, unrecorded: PLC) -> None:
        unrecorded._search_refuses_restart = False
        with pytest.raises(UnrecordedScansError):
            unrecorded.cause(Slow.Acc.name)

    def test_it_ends_the_search_and_says_so(self, monkeypatch: pytest.MonkeyPatch) -> None:
        real = coast_run.coast_until

        def then_ask(plc: PLC, *args: Any, **kwargs: Any) -> Any:
            real(plc, *args, **kwargs)
            raise SearchQuestionRefused(UnrecordedScansError(1, 2, 3))

        monkeypatch.setattr(pilot_coast, "coast_until", then_ask)
        plan = _slow_lamp(30_000).how(Lamp, wall_clock_s=None)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("a question was refused: scans 1..2 include")
        assert "physical link" not in plan.reason

    def test_what_the_terminal_reads_cannot_end_a_search_again(self, monkeypatch) -> None:
        # The clock is out, and a BaseException out of how() would reach the caller raw.
        monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)
        terminal_polled = []
        real = pilot_module._stopped_events

        def stopped_events(state: Any, *args: Any, **kwargs: Any) -> Any:
            terminal_polled.append(state.work._coast_memo.check_limits)
            return real(state, *args, **kwargs)

        monkeypatch.setattr(pilot_module, "_stopped_events", stopped_events)
        plan = _slow_lamp(30_000).how(Lamp, wall_clock_s=0.0)
        assert plan.status is PlanStatus.STOPPED
        assert terminal_polled == [None]


class TestTheClock:
    @pytest.fixture(autouse=True)
    def _clock_on(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(budget, "WALL_CLOCK_ENABLED", True)

    def test_out_of_time_is_its_own_terminal(self) -> None:
        plan = _slow_lamp(30_000).how(Lamp, wall_clock_s=0.0)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("wall-clock budget exhausted (0 s)")
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
        assert plan.reason.startswith("wall-clock budget exhausted (1 s)")

    def test_a_theory_can_end_by_the_clock(self) -> None:
        assert TheoryTermination.WALL_CLOCK.value == "wall-clock"
