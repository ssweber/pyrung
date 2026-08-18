"""Requirement schedule compilation contracts."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Program, copy, rung
from pyrung.core.analysis.pilot.requirement_recovery import (
    compile_scalar_schedule,
    guard_alternatives,
)
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.crossing import Cmp

ScheduleEnabled = Bool("ScheduleEnabled", external=True)
SchedulePreset = Int("SchedulePreset")
ConfiguredPreset = Int("ConfiguredPreset", default=20)
ScheduleSink = Int("ScheduleSink")
ConfiguredSink = Int("ConfiguredSink")

with Program() as schedule_logic:
    with rung(ScheduleEnabled):
        copy(SchedulePreset, ScheduleSink)
        copy(ConfiguredPreset, ConfiguredSink)


def _requirement(condition: Cmp, *, permits_assignment: bool = True):
    return SimpleNamespace(
        condition=condition,
        status=RequirementStatus.ACTIVE,
        phase=RequirementPhase.STEADY,
        checkpoint_owner="checkpoint-0",
        source_world_key=("source",),
        permits_assignment=permits_assignment,
        identity=(condition, permits_assignment),
    )


def test_compatible_same_tag_constraints_compile_to_one_intersection_value() -> None:
    plc = PLC(schedule_logic)
    requirements = (
        _requirement(Cmp(SchedulePreset.name, ">", 10)),
        _requirement(Cmp(SchedulePreset.name, "<=", 20)),
    )

    result = compile_scalar_schedule(requirements, plc, guard=ScheduleEnabled)

    assert result.detail == ""
    assert result.schedule is not None
    assert result.schedule.assignments == ((SchedulePreset.name, 11),)
    assert len(result.schedule.pilot_rungs) == 1
    assert result.schedule.pilot_rungs[0].dest == SchedulePreset.name
    assert result.schedule.pilot_rungs[0].value == 11


def test_incompatible_exact_constraints_reject_the_schedule() -> None:
    plc = PLC(schedule_logic)
    requirements = (
        _requirement(Cmp(SchedulePreset.name, ">", 10)),
        _requirement(Cmp(SchedulePreset.name, "<", 5)),
    )

    result = compile_scalar_schedule(requirements, plc, guard=ScheduleEnabled)

    assert result.schedule is None
    assert result.detail == f"incompatible scalar requirements for {SchedulePreset.name!r}"


def test_unsatisfied_authoritative_operand_rejects_direct_assignment() -> None:
    plc = PLC(schedule_logic)

    result = compile_scalar_schedule(
        (_requirement(Cmp(SchedulePreset.name, ">", 10), permits_assignment=False),),
        plc,
        guard=ScheduleEnabled,
    )

    assert result.schedule is None
    assert result.detail == "an unsatisfied authoritative operand forbids direct assignment"


def test_satisfied_authoritative_condition_permits_retry_without_assignment() -> None:
    plc = PLC(schedule_logic)

    result = compile_scalar_schedule(
        (
            _requirement(
                Cmp(ConfiguredPreset.name, ">=", ConfiguredPreset.default),
                permits_assignment=False,
            ),
        ),
        plc,
        guard=ScheduleEnabled,
    )

    assert result.detail == ""
    assert result.schedule is not None
    assert result.schedule.assignments == ()
    assert result.schedule.pilot_rungs == ()


def test_satisfied_configured_and_unsatisfied_adjustable_compile_only_adjustable() -> None:
    plc = PLC(schedule_logic)
    requirements = (
        _requirement(
            Cmp(ConfiguredPreset.name, ">=", ConfiguredPreset.default),
            permits_assignment=False,
        ),
        _requirement(Cmp(SchedulePreset.name, ">", 10)),
    )

    result = compile_scalar_schedule(requirements, plc, guard=ScheduleEnabled)

    assert result.detail == ""
    assert result.schedule is not None
    assert result.schedule.assignments == ((SchedulePreset.name, 11),)
    assert tuple((rung.dest, rung.value) for rung in result.schedule.pilot_rungs) == (
        (SchedulePreset.name, 11),
    )


def test_guard_any_remains_separate_repair_alternatives() -> None:
    deadline = object()
    left = GuardRequirementAtom(Cmp("Left", "==", True), (), deadline, (0,))
    right = GuardRequirementAtom(Cmp("Right", "==", True), (), deadline, (1,))
    condition = GuardRequirementExpr(GuardLogic.ANY, (left, right))

    assert guard_alternatives(condition) == ((left,), (right,))
