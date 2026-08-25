"""Pure Stage 3 schedule compilation contracts."""

from __future__ import annotations

from types import SimpleNamespace

from pyrung import PLC, Bool, Int, Program, copy, rung
from pyrung.core.analysis.pilot.intrascan_schedule import (
    compile_scalar_schedule as compile_intrascan_scalar_schedule,
)
from pyrung.core.analysis.pilot.intrascan_schedule import iter_guard_alternatives
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.crossing import Cmp

ScheduleActive = Bool("IntrascanScheduleActive", external=True)
ScheduleFirst = Int("IntrascanScheduleFirst")
ScheduleSecond = Int("IntrascanScheduleSecond")
ScheduleConsumer = Int("IntrascanScheduleConsumer")

with Program() as schedule_program:
    with rung(ScheduleActive):
        copy(ScheduleFirst, ScheduleConsumer)
        copy(ScheduleSecond, ScheduleConsumer)


def _atom(name: str, deadline: object, path: int) -> GuardRequirementAtom:
    return GuardRequirementAtom(Cmp(name, "==", True), (), deadline, (path,))


def _scalar_requirement(condition: Cmp):
    return SimpleNamespace(
        condition=condition,
        status=RequirementStatus.ACTIVE,
        phase=RequirementPhase.STEADY,
        checkpoint_owner="intrascan-checkpoint",
        source_world_key=("intrascan-source",),
        permits_assignment=True,
        identity=(condition,),
    )


def test_nested_all_any_yields_only_joint_sibling_alternatives() -> None:
    first_deadline = object()
    second_deadline = object()
    common_deadline = object()
    first = _atom("IntrascanFirstPath", first_deadline, 0)
    second = _atom("IntrascanSecondPath", second_deadline, 1)
    common = _atom("IntrascanCommonPermit", common_deadline, 2)
    condition = GuardRequirementExpr(
        GuardLogic.ALL,
        (
            GuardRequirementExpr(GuardLogic.ANY, (first, second)),
            common,
        ),
    )

    alternatives = tuple(iter_guard_alternatives(condition))

    assert alternatives == ((first, common), (second, common))
    assert all(len(alternative) == 2 for alternative in alternatives)
    assert all(
        first not in alternative or second not in alternative for alternative in alternatives
    )
    assert [atom.deadline for atom in alternatives[0]] == [first_deadline, common_deadline]
    assert [atom.deadline for atom in alternatives[1]] == [second_deadline, common_deadline]


def test_nested_any_all_yields_joint_branch_and_atomic_sibling_without_superset() -> None:
    first_deadline = object()
    second_deadline = object()
    sibling_deadline = object()
    first = _atom("IntrascanJointFirst", first_deadline, 0)
    second = _atom("IntrascanJointSecond", second_deadline, 1)
    sibling = _atom("IntrascanSibling", sibling_deadline, 2)
    condition = GuardRequirementExpr(
        GuardLogic.ANY,
        (
            GuardRequirementExpr(GuardLogic.ALL, (first, second)),
            sibling,
        ),
    )

    alternatives = tuple(iter_guard_alternatives(condition))

    assert alternatives == ((first, second), (sibling,))
    assert (first, second, sibling) not in alternatives
    assert [atom.deadline for atom in alternatives[0]] == [first_deadline, second_deadline]
    assert alternatives[1][0].deadline is sibling_deadline


def test_intrascan_scalar_compiler_materializes_one_exact_schedule() -> None:
    plc = PLC(schedule_program)
    requirements = (
        _scalar_requirement(Cmp(ScheduleFirst.name, ">", 10)),
        _scalar_requirement(Cmp(ScheduleFirst.name, "<=", 20)),
        _scalar_requirement(Cmp(ScheduleSecond.name, "==", 4)),
    )

    direct = compile_intrascan_scalar_schedule(requirements, plc, guard=ScheduleActive)
    assert direct.schedule is not None
    assert direct.detail == ""
    assert direct.schedule.checkpoint_owner == "intrascan-checkpoint"
    assert direct.schedule.source_world_key == ("intrascan-source",)
    assert direct.schedule.phase is RequirementPhase.STEADY
    assert all(item.guard is ScheduleActive for item in direct.schedule.pilot_rungs)
    assert direct.schedule.assignments == (
        (ScheduleFirst.name, 11),
        (ScheduleSecond.name, 4),
    )
