"""What a how() may spend: the ledger in work units and the clock behind it."""

from __future__ import annotations

import warnings
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

import pytest

from pyrung import (
    PLC,
    Block,
    Bool,
    Int,
    ModbusTcpTarget,
    Physical,
    Program,
    Rung,
    TagType,
    Timer,
    latch,
    on_delay,
    out,
    receive,
    run_function,
    system,
)
from pyrung.circuitpy import codegen
from pyrung.core import coast_run, runner, scan_trace
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot import budget
from pyrung.core.analysis.pilot import coast as pilot_coast
from pyrung.core.analysis.pilot import pilot as pilot_module
from pyrung.core.analysis.pilot.budget import SearchBudget
from pyrung.core.analysis.pilot.causal import _shared_cause, action_caused_change
from pyrung.core.analysis.pilot.working_theory import TheoryTermination
from pyrung.core.rung_firings import SearchQuestionRefused, UnrecordedScansError
from pyrung.core.synthesis import Synthesis

Slow = Timer.clone("Slow")
Lamp = Bool("Lamp")
y_Done = Bool("y_Done")
Seen = Int("Seen")
Never = Bool("Never")
Busy = Bool("Busy")
Got = Bool("Got")
Err = Bool("Err")
Code = Int("Code")
DS = Block("DS", TagType.INT, 1, 20)


def _receipt(*scans: int, epoch: str = "e", waited: tuple[int, ...] = ()) -> SimpleNamespace:
    return SimpleNamespace(
        spans=(
            SimpleNamespace(
                epoch=SimpleNamespace(reference=epoch), kernel_scan_ids=scans, loop_priced=waited
            ),
        )
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

    def test_an_interpreted_scan_a_coast_waited_through_is_priced_as_a_loop_scan(self) -> None:
        ledger = SearchBudget()
        ledger.charge_execution(_receipt(*range(1, 104), waited=tuple(range(3, 103))))
        assert ledger.spent == 3 + 1

    def test_a_how_cannot_run_free_in_the_loop(self) -> None:
        # Its 2,000,000 loop scans would be 20,000 units.  The seek's own budget is logical,
        # so what ends it is a search that has spent its work.
        plan = _slow_lamp(2_000_000).how(Lamp, max_scans=200)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("budget exhausted")


class TestTheLoopDoesNotMoveTheLedger:
    # A wait's scans are priced by where they fall, so a search spends the same whichever
    # scans the loop runs.  With no trace allowed the loop never runs, and every scan is
    # interpreted.

    @pytest.mark.parametrize(
        ("preset", "max_scans", "status"),
        [(300, 3, PlanStatus.STOPPED), (3000, 20, PlanStatus.REACHED)],
        ids=["out of work", "a wait of 3000 scans on 20 units"],
    )
    def test_a_search_spends_the_same_without_the_loop(
        self, monkeypatch: pytest.MonkeyPatch, preset: int, max_scans: int, status: PlanStatus
    ) -> None:
        looped = _slow_lamp(preset).how(Lamp, max_scans=max_scans)
        monkeypatch.setattr(coast_run, "TRACE_ROOM", 10**9)
        loop_scans = scan_trace.TOTALS["loop scans"]
        stepped = _slow_lamp(preset).how(Lamp, max_scans=max_scans)
        assert scan_trace.TOTALS["loop scans"] == loop_scans
        assert looped.status is status
        assert (stepped.status, stepped.reason, stepped.total_scans) == (
            looped.status,
            looped.reason,
            looped.total_scans,
        )


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
        monkeypatch.setattr(budget, "_MOST_POLLS_PER_COUNT", 1)
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


def _unreachable(rung: Callable[[], None] = lambda: None) -> PLC:
    with Program() as logic:
        with Rung():
            on_delay(Slow, preset=50_000_000)
        with Rung(Slow.Done):
            out(Lamp)
        rung()
    return PLC(logic, dt=0.001)


def _receives() -> None:
    with Rung(Never):
        receive(
            target=ModbusTcpTarget(name="peer", ip="127.0.0.1", port=1),
            remote_start="DS1",
            dest=DS.select(11, 12),
            receiving=Busy,
            success=Got,
            error=Err,
            exception_response=Code,
        )


def _writes_a_read_only_system_tag() -> None:
    with Rung(Never):
        latch(system.sys.always_on)


class TestAWaitNoLoopMayRun:
    # Each scan of it costs a unit, so the ledger stops a target it cannot reach.  At a
    # hundredth a scan, how() would run on until its clock.

    @pytest.mark.parametrize(
        "rung",
        [_receives, _writes_a_read_only_system_tag],
        ids=["a Modbus receive", "a write to a read-only system tag"],
    )
    def test_a_program_with_no_kernel_stops_at_the_ledger(self, rung: Any) -> None:
        assert _unreachable(rung)._compiled_replay_supported_kernel() is None
        plan = _unreachable(rung).how(Lamp, max_scans=300, wall_clock_s=60)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("budget exhausted")

    @pytest.mark.parametrize(
        "error", [NotImplementedError("a gap"), RuntimeError("a bug")], ids=["gap", "raise"]
    )
    def test_a_program_that_does_not_compile_stops_at_the_ledger(
        self, monkeypatch: pytest.MonkeyPatch, error: Exception
    ) -> None:
        def refuse(*_a: Any, **_k: Any) -> Any:
            raise error

        monkeypatch.setattr(codegen, "compile_kernel", refuse)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            plan = _unreachable().how(Lamp, max_scans=300, wall_clock_s=60)
        assert plan.status is PlanStatus.STOPPED
        assert plan.reason.startswith("budget exhausted")

    def _out_of_work(self, plc: PLC, predicate: Any = None) -> dict[str, int]:
        coast_run.memo_of(plc).work_left = lambda: 5
        stats: dict[str, int] = {}
        coast_run.coast_until(
            plc,
            predicate or (lambda _s: False),
            budget=10**6,
            predicate_reads=None if predicate else frozenset(),
            stats=stats,
            loop_scans_per_unit=100,
        )
        return stats

    def test_a_call_in_the_overlay_that_keeps_state_pays_by_the_scan(self) -> None:
        with Program() as overlay:
            with Rung():
                run_function(_Stateful(), outs={"seen": Seen})
        plc = _unreachable()
        plc._synthesis = Synthesis(plant=list(overlay.rungs))
        stats = self._out_of_work(plc)
        assert (stats.get("out_of_work"), stats["logical_scans"]) == (1, 5)

    def test_a_stop_predicate_the_loop_cannot_watch_pays_by_the_scan(self) -> None:
        stats = self._out_of_work(_unreachable(), lambda state: state.scan_id < 0)
        assert (stats.get("out_of_work"), stats["logical_scans"]) == (1, 5)

    def test_a_stop_predicate_that_comes_to_read_what_the_loop_cannot_watch(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It reads only Lamp until Lamp turns on, and then a tag that moves on every scan.  The
        # scans from there cost a unit each, and the loop runs the scans before.
        counter = system.sys.scan_counter.name

        def predicate(state: Any) -> bool:
            return bool(state.tags.get(Lamp.name)) and state.tags.get(counter, 0) < 0

        def wait() -> dict[str, int]:
            plc = _slow_lamp(50)
            coast_run.memo_of(plc).work_left = lambda: 20
            stats: dict[str, int] = {}
            coast_run.coast_until(plc, predicate, budget=2000, stats=stats, loop_scans_per_unit=100)
            assert plc.state.tags[Lamp.name]
            return stats

        looped = wait()
        monkeypatch.setattr(coast_run, "TRACE_ROOM", 10**9)
        stepped = wait()
        assert looped["stretched_scans"] > 0 and stepped["stretched_scans"] == 0
        keys = ("logical_scans", "unit_scans", "out_of_work")
        assert [looped.get(key) for key in keys] == [stepped.get(key) for key in keys]
        assert stepped["out_of_work"] == 1


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


class TestTheMemoryPoll:
    def _polls_until_it_raises(self, monkeypatch: pytest.MonkeyPatch, rate: int) -> tuple[int, int]:
        """The polls and the blocks held when a search adding *rate* blocks a poll is stopped."""
        monkeypatch.setattr(budget, "MAX_SEARCH_BLOCKS", 1_000_000)
        counts = 0
        held = 0

        def blocks() -> int:
            nonlocal counts
            counts += 1
            return held

        monkeypatch.setattr(budget.sys, "getallocatedblocks", blocks)
        check = budget.search_limits(None)
        counts = 0
        with pytest.raises(budget.SearchOutOfMemory):
            for _ in range(10_000_000):
                held += rate
                check()
        return counts, held

    @pytest.mark.parametrize("rate", [20_000, 200_000, 2_000_000])
    def test_a_wide_program_is_stopped_at_the_cap(
        self, monkeypatch: pytest.MonkeyPatch, rate: int
    ) -> None:
        _counts, held = self._polls_until_it_raises(monkeypatch, rate)
        assert held <= 1_000_000 + rate

    def test_a_narrow_program_counts_rarely(self, monkeypatch: pytest.MonkeyPatch) -> None:
        counts, held = self._polls_until_it_raises(monkeypatch, 23)
        assert held <= 1_000_000 + 23
        polls = held // 23
        # Every 64th poll, and every poll only for the last stretch before the cap.
        assert counts < polls / 50

    def test_a_rate_that_jumps_shows_within_the_most_polls(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(budget, "MAX_SEARCH_BLOCKS", 1_000_000)
        held = 0
        monkeypatch.setattr(budget.sys, "getallocatedblocks", lambda: held)
        check = budget.search_limits(None)
        for _ in range(1000):
            held += 1
            check()
        with pytest.raises(budget.SearchOutOfMemory):
            for _ in range(1000):
                held += 20_000
                check()
        assert held <= 1_000_000 + 64 * 20_000


class TestATerminal:
    # What a terminal reads is no search's: a limit there would end the search a second time.

    @pytest.mark.parametrize(
        ("max_scans", "reason"),
        [(0, "budget exhausted"), (3000, None)],
        ids=["the budget's terminal", "reaching the target"],
    )
    def test_a_limit_cannot_fire_inside_one(
        self, monkeypatch: pytest.MonkeyPatch, max_scans: int, reason: str | None
    ) -> None:
        stuck = 0
        real_stuck = pilot_module._stuck_event
        real_payload = pilot_module._knowledge_payload

        def counting(*args: Any, **kwargs: Any) -> Any:
            nonlocal stuck
            stuck += 1
            return real_stuck(*args, **kwargs)

        def payload(state: Any, *args: Any, **kwargs: Any) -> Any:
            # Stands for any read here that polls the limits, a settle for one.
            memo = state.work._coast_memo
            if memo is not None and memo.check_limits is not None:
                raise budget.SearchOutOfMemory
            return real_payload(state, *args, **kwargs)

        monkeypatch.setattr(pilot_module, "_stuck_event", counting)
        monkeypatch.setattr(pilot_module, "_knowledge_payload", payload)
        plan = _slow_lamp(50).how(Lamp, max_scans=max_scans, wall_clock_s=None)
        if reason is None:
            assert plan.status is PlanStatus.REACHED
            assert stuck == 0
        else:
            assert plan.status is PlanStatus.STOPPED
            assert plan.reason.startswith(reason)
            assert stuck == 1


class TestARefusedQuestion:
    # Scans the search cannot afford to read are not evidence that nothing happened in them.

    @pytest.fixture
    def unrecorded(self, monkeypatch: pytest.MonkeyPatch) -> PLC:
        """A fork in a search, whose scans have no records and may not get them."""
        monkeypatch.setattr(coast_run, "MAX_RECORD_RUNS", 0)
        monkeypatch.setattr(runner, "SETTLE_SCAN_LIMIT", 0)
        plc = _slow_lamp(100_000).fork()
        coast_run.coast_until(plc, lambda _state: False, budget=5000)
        assert plc._unrecorded_scans
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
