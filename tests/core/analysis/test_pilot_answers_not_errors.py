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
    latch,
    on_delay,
)
from pyrung.core.analysis.graph import PlanStatus
from pyrung.core.analysis.pilot.api import pilot_events
from tests.fixtures import pilot_bootstrap_guard_overwrite as guard

BOUND: dict[str, Any] = {"wall_clock_s": 30.0, "max_scans": 3000}


def test_a_scan_already_past_the_entry_stops_with_a_reason() -> None:
    """Scan 0 gives the target and the next scan overwrites it: the re-run needs a rewind."""
    plc = PLC(guard.logic, dt=0.01)
    plc.step()

    plan = plc.how(guard.SequenceState == guard.TARGET, **BOUND)

    assert plan.status is PlanStatus.STOPPED
    assert "lost track of its own evidence" in (plan.reason or "")


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
        assert plan.status in (PlanStatus.REACHED, PlanStatus.STOPPED), plan.reason

    def test_packml_with_an_avoid_on_a_state_it_starts_in(self) -> None:
        plc = PLC(packml, dt=0.01)
        plan = plc.how(
            StateCurrent == PackmlStates.COMPLETED.default,
            avoid=lambda state: state[StateCurrent.name] == 0,
            **BOUND,
        )
        assert plan.status in (PlanStatus.REACHED, PlanStatus.STOPPED), plan.reason


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

    def test_a_tag_operand_no_rung_uses(self) -> None:
        program, watch, _other, _hit = self._program()
        unused = Int("Unused", default=0)
        plan = PLC(program, dt=0.01).how(watch.Acc >= unused, **BOUND)
        assert plan.status is PlanStatus.REACHED, plan.reason

    def test_a_tag_operand_no_rung_uses_in_the_event_stream(self) -> None:
        program, watch, _other, _hit = self._program()
        unused = Int("Unused", default=0)
        events = list(pilot_events(PLC(program, dt=0.01), watch.Acc >= unused, max_scans=3000))
        assert events[-1].kind == "finished"

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

    @pytest.mark.parametrize("goal", ["joint", "relational"])
    def test_a_goal_on_a_tag_no_rung_uses_stops_and_says_so(self, goal: str) -> None:
        program, watch, _other, hit = self._program()
        flag, count = Bool("Absent"), Int("AbsentCount")
        conditions = (And(hit, ~flag),) if goal == "joint" else (count >= 5,)
        plan = PLC(program, dt=0.01).how(*conditions, **BOUND)
        assert plan.status is PlanStatus.STOPPED
        assert "no rung uses" in plan.reason

    def test_or_is_a_clear_error(self) -> None:
        program, watch, _other, hit = self._program()
        with pytest.raises(ValueError, match="no either-or target"):
            PLC(program, dt=0.01).how(Or(hit, watch.Done), **BOUND)


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
