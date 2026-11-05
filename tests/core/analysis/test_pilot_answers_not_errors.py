"""how() answers: a plan, a proof or a stop with a reason, never a Python error from inside."""

from __future__ import annotations

from typing import Any

import pytest

from examples.learn import assignment
from examples.packml_bench import S as PackmlStates
from examples.packml_bench import StateCurrent
from examples.packml_bench import logic as packml
from pyrung import (
    PLC,
    And,
    Block,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    Timer,
    copy,
    fill,
    latch,
    on_delay,
    system,
)
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot.api import pilot_events
from tests.fixtures import pilot_bootstrap_guard_overwrite as guard

BOUND: dict[str, Any] = {"wall_clock_s": 30.0, "max_scans": 3000}


def test_a_scan_already_past_the_entry_stops_with_a_reason() -> None:
    """Scan 0 gives the target and the next scan overwrites it, so the state must be cleared."""
    plc = PLC(guard.logic, dt=0.01)
    plc.step()

    plan = plc.how(guard.SequenceState == guard.TARGET, **BOUND)

    assert plan.status is PlanStatus.STOPPED
    assert (plan.reason or "").startswith("No productive next action was found")
    assert plan.restart.clear == {guard.SequenceState.name: 0}


def test_the_same_entry_from_cold_still_reaches() -> None:
    plan = PLC(guard.logic, dt=0.01).how(guard.SequenceState == guard.TARGET, **BOUND)
    assert plan.status is PlanStatus.REACHED, plan.reason


@pytest.mark.parametrize("warm", [0, 1, 2])
def test_assignment_warmed_reaches(warm: int) -> None:
    """Two warm scans once raised TheoryInvariantError here."""
    plc = PLC(assignment.logic, dt=0.01)
    for _ in range(warm):
        plc.step()
    plan = plc.how(assignment.CycleCount == 5, **BOUND)
    assert plan.status is PlanStatus.REACHED, plan.reason


class TestArithmeticInARung:
    def _program(self) -> tuple[Program, Any, Any, Any]:
        watch = Timer.clone("Watch")
        limit = Int("Limit", default=20)
        enable = Bool("Enable", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as program:
            with Rung(enable):
                on_delay(watch, 1000)
            with Rung(watch.Acc + 1 >= limit):
                latch(hit)
        return program, hit, watch, enable

    def test_a_guard_with_arithmetic_is_no_unsupported_construct(self) -> None:
        program, hit, _watch, _enable = self._program()
        plan = PLC(program, dt=0.01).how(hit, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason
        assert plan.fork is not None
        assert plan.fork.state.tags[hit.name]

    def test_packml_with_an_avoid_on_a_state_it_starts_in(self) -> None:
        plc = PLC(packml, dt=0.01)
        plan = plc.how(
            StateCurrent == PackmlStates.COMPLETED.default,
            avoid=lambda state: state[StateCurrent.name] == 0,
            **BOUND,
        )
        assert plan.status is PlanStatus.REACHED, plan.reason


class TestTargetForms:
    def _program(self) -> tuple[Program, Any, Any, Any]:
        watch = Timer.clone("Watch")
        enable = Bool("Enable", external=True)
        other = Bool("Other", external=True)
        hit = Bool("Hit")
        with Program(strict=False) as program:
            with Rung(enable):
                on_delay(watch, 100)
            with Rung(watch.Done, other):
                latch(hit)
        return program, watch, other, hit

    def test_a_tag_operand_no_rung_uses_is_a_clear_error(self) -> None:
        """Its value could be a default, a force or a patch: no literal stands in for it."""
        program, watch, _other, _hit = self._program()
        setpoint = Int("Sp", default=500)
        with pytest.raises(ValueError, match="compared with Sp, and no rung uses that tag"):
            PLC(program, dt=0.01).how(watch.Acc >= setpoint, **BOUND)

    def test_a_tag_operand_no_rung_uses_in_the_event_stream(self) -> None:
        program, watch, _other, _hit = self._program()
        setpoint = Int("Sp", default=500)
        with pytest.raises(ValueError, match="compared with Sp, and no rung uses that tag"):
            list(pilot_events(PLC(program, dt=0.01), watch.Acc >= setpoint, max_scans=3000))

    def test_a_tag_operand_a_rung_reads_is_judged_live(self) -> None:
        watch = Timer.clone("Watch")
        setpoint = Int("Sp", default=500)
        enable, hit = Bool("Enable", external=True), Bool("Hit")
        with Program(strict=False) as program:
            with Rung(enable):
                on_delay(watch, 2000)
            with Rung(watch.Acc >= setpoint):
                latch(hit)
        plan = PLC(program, dt=0.01).how(watch.Acc >= setpoint, **BOUND)
        if plan.status is PlanStatus.REACHED:
            assert plan.fork is not None
            assert plan.fork.state.tags[watch.Acc.name] >= 500
        else:
            assert plan.status is PlanStatus.STOPPED, plan.reason

    def test_arithmetic_in_the_target_is_a_clear_error(self) -> None:
        program, watch, _other, _hit = self._program()
        limit = Int("Limit", default=5)
        with pytest.raises(ValueError, match="compared with arithmetic"):
            PLC(program, dt=0.01).how(watch.Acc >= limit + 1, **BOUND)

    def test_and_is_several_targets(self) -> None:
        program, watch, _other, hit = self._program()
        plan = PLC(program, dt=0.01).how(And(hit, watch.Done), **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason
        assert plan.fork is not None
        assert plan.fork.state.tags[hit.name] and plan.fork.state.tags[watch.Done.name]

    def test_a_normally_closed_member_of_and(self) -> None:
        program, watch, other, hit = self._program()
        plan = PLC(program, dt=0.01).how(And(hit, ~other), **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason
        assert plan.fork is not None
        assert plan.fork.state.tags[hit.name] and not plan.fork.state.tags[other.name]

    def test_a_goal_member_no_rung_uses_holds_at_its_default(self) -> None:
        program, _watch, other, hit = self._program()
        absent = Bool("Absent")
        plan = PLC(program, dt=0.01).how(And(hit, ~absent, other), **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_goal_member_no_rung_uses_never_moves(self) -> None:
        program, _watch, _other, hit = self._program()
        plan = PLC(program, dt=0.01).how(hit, Bool("Absent"), **BOUND)
        assert plan.status is PlanStatus.STOPPED, plan.reason

    def test_a_goal_member_no_rung_uses_at_a_default_of_on(self) -> None:
        program, _watch, _other, hit = self._program()
        absent = Bool("Absent", default=True)
        assert PLC(program, dt=0.01).how(hit, ~absent, **BOUND).status is PlanStatus.STOPPED

    def test_a_relational_goal_on_a_tag_no_rung_uses(self) -> None:
        program, _watch, _other, _hit = self._program()
        plan = PLC(program, dt=0.01).how(Int("AbsentCount") >= 5, **BOUND)
        assert plan.status is PlanStatus.STOPPED, plan.reason

    def test_a_system_bit_in_a_goal_is_no_unused_tag(self) -> None:
        program, _watch, other, hit = self._program()
        plan = PLC(program, dt=0.01).how(hit, other, system.sys.always_on, **BOUND)
        assert plan.status is PlanStatus.STOPPED
        assert "no rung uses" not in (plan.reason or "")

    def test_or_is_a_clear_error(self) -> None:
        program, watch, _other, hit = self._program()
        with pytest.raises(ValueError, match="no either-or target"):
            PLC(program, dt=0.01).how(Or(hit, watch.Done), **BOUND)


class TestGoalsOnSlotsARangeWrites:
    """A slot first named by the goal is written through a range or pointer: rungs do use it."""

    def _reach(self, write: Any, goal: Any) -> PlanStatus:
        go, done = Bool("Go", external=True), Bool("X")
        with Program(strict=False) as program:
            with Rung(go):
                write()
                latch(done)
        return PLC(program, dt=0.01).how(*goal(done), **BOUND).status

    def test_fill(self) -> None:
        bank = Block("B", TagType.INT, 1, 5)
        status = self._reach(lambda: fill(7, bank.select(1, 3)), lambda done: (bank[2] == 7, done))
        assert status is PlanStatus.REACHED

    def test_a_pointer(self) -> None:
        bank = Block("B", TagType.INT, 1, 5)
        index = Int("Idx", external=True, default=2)
        status = self._reach(lambda: copy(7, bank[index]), lambda done: (bank[2] == 7, done))
        assert status is PlanStatus.REACHED

    def test_a_pointer_range(self) -> None:
        bank = Block("B", TagType.INT, 1, 5)
        index = Int("Idx", external=True, default=2)
        status = self._reach(
            lambda: fill(7, bank.select(index, index + 1)), lambda done: (bank[2] == 7, done)
        )
        assert status is PlanStatus.REACHED

    def test_a_relation_on_a_slot_says_no_false_reason(self) -> None:
        go, done = Bool("Go", external=True), Bool("X")
        bank = Block("B", TagType.INT, 1, 5)
        with Program(strict=False) as program:
            with Rung(go):
                fill(7, bank.select(1, 3))
                latch(done)
        plan = PLC(program, dt=0.01).how(bank[3] >= 5, **BOUND)
        assert "no rung uses" not in (plan.reason or "")


class TestOpaqueAvoids:
    def _program(self) -> tuple[Program, Any, Any]:
        bank = Block("Bank", TagType.INT, 1, 3)
        index = Int("Idx", external=True, default=1)
        go = Bool("Go", external=True)
        done = Bool("Done")
        with Program(strict=False) as program:
            with Rung(go):
                copy(5, bank[index])
                latch(done)
        return program, done, bank[index]

    def test_a_pointer_avoid_the_route_stays_clear_of(self) -> None:
        program, done, slot = self._program()
        plan = PLC(program, dt=0.01).how(done, avoid=slot >= 9000, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_pointer_avoid_the_route_trips(self) -> None:
        program, done, slot = self._program()
        plan = PLC(program, dt=0.01).how(done, avoid=slot == 5, **BOUND)
        assert plan.status is not PlanStatus.REACHED, plan.reason


class TestOpaqueAvoidGoesTrueMidCoast:
    """The violation comes and goes while a timer runs, with no input moving."""

    def _program(self) -> tuple[Program, Any, Any, Any, Any]:
        timer = Timer.clone("T")
        bank = Block("B", TagType.INT, 1, 3)
        plain = Int("A")
        index = Int("Idx", external=True, default=1)
        run, hit = Bool("Run", external=True), Bool("Hit")
        with Program(strict=False) as program:
            with Rung(run):
                on_delay(timer, 2000)
            with Rung(timer.Acc >= 500, timer.Acc < 600):
                copy(9, bank[1])
                copy(9, plain)
            with Rung(timer.Acc >= 600):
                copy(0, bank[1])
                copy(0, plain)
            with Rung(timer.Done):
                latch(hit)
        return program, hit, bank, plain, index

    def test_the_route_reaches_without_the_avoid(self) -> None:
        program, hit, *_ = self._program()
        assert PLC(program, dt=0.01).how(hit, **BOUND).status is PlanStatus.REACHED

    @pytest.mark.parametrize(
        "avoid",
        [
            lambda bank, plain, index: bank[index] >= 5,
            lambda bank, plain, index: plain + 1 >= 6,
            lambda bank, plain, index: Or(bank[index] >= 5, plain == -1),
        ],
        ids=["pointer", "expression", "pointer in an or"],
    )
    def test_it_is_seen(self, avoid: Any) -> None:
        program, hit, bank, plain, index = self._program()
        plan = PLC(program, dt=0.01).how(hit, avoid=avoid(bank, plain, index), **BOUND)
        assert plan.status is not PlanStatus.REACHED, plan.reason

    @pytest.mark.parametrize(
        ("avoid", "said"),
        [
            (lambda bank, plain, index: bank[index] >= 5, "B[Idx] >= 5"),
            (lambda bank, plain, index: plain + 1 >= 6, "(A + 1) >= 6"),
        ],
        ids=["pointer", "expression"],
    )
    def test_the_reason_names_it(self, avoid: Any, said: str) -> None:
        program, hit, bank, plain, index = self._program()
        plan = PLC(program, dt=0.01).how(hit, avoid=avoid(bank, plain, index), **BOUND)
        assert said in (plan.reason or ""), plan.reason
