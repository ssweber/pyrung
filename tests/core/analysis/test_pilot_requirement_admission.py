"""Current-world admission for retained temporal requirements."""

from __future__ import annotations

from types import SimpleNamespace
from typing import cast

from pyrung.core.analysis.pilot.requirement_admission import (
    actions_preserve_active_requirements,
    active_requirement_violations,
    requirement_condition_holds,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.crossing import Cmp


def _atom(
    tag: str,
    value: object,
    *,
    authority: OperandAuthority = OperandAuthority.CONFIGURED,
) -> GuardRequirementAtom:
    return GuardRequirementAtom(
        Cmp(tag, "==", value),
        (),
        SimpleNamespace(),
        (),
        operand_authority=authority,
    )


def _requirement(
    condition: GuardRequirementAtom | GuardRequirementExpr,
    *,
    authority: OperandAuthority = OperandAuthority.CONFIGURED,
) -> ActiveRequirement:
    return cast(
        ActiveRequirement,
        SimpleNamespace(
            condition=condition,
            status=RequirementStatus.ACTIVE,
            phase=RequirementPhase.STEADY,
            operand_authority=authority,
        ),
    )


def test_compound_requirement_uses_exact_all_and_any_truth() -> None:
    ready = _atom("AdmissionReady", True)
    mode = _atom("AdmissionMode", 2)

    assert requirement_condition_holds(
        GuardRequirementExpr(GuardLogic.ALL, (ready, mode)),
        {"AdmissionReady": True, "AdmissionMode": 2},
    )
    assert not requirement_condition_holds(
        GuardRequirementExpr(GuardLogic.ALL, (ready, mode)),
        {"AdmissionReady": True, "AdmissionMode": 1},
    )
    assert requirement_condition_holds(
        GuardRequirementExpr(GuardLogic.ANY, (ready, mode)),
        {"AdmissionReady": False, "AdmissionMode": 2},
    )


def test_only_authoritative_true_to_false_transition_is_a_violation() -> None:
    configured = _requirement(_atom("AdmissionPermit", True))
    adjustable = _requirement(
        _atom("AdmissionAdjustable", True, authority=OperandAuthority.ADJUSTABLE),
        authority=OperandAuthority.ADJUSTABLE,
    )

    violations = active_requirement_violations(
        (configured, adjustable),
        {"AdmissionPermit": True, "AdmissionAdjustable": True},
        {"AdmissionPermit": False, "AdmissionAdjustable": False},
    )

    assert violations == (configured,)
    assert (
        active_requirement_violations(
            (configured,),
            {"AdmissionPermit": False},
            {"AdmissionPermit": True},
        )
        == ()
    )


def test_action_cannot_assign_an_authoritative_requirement_operand() -> None:
    requirement = _requirement(_atom("AdmissionHeld", True))
    snapshot = {"AdmissionHeld": True, "AdmissionOther": False}

    assert not actions_preserve_active_requirements(
        (requirement,),
        snapshot,
        (("AdmissionHeld", False),),
    )
    assert actions_preserve_active_requirements(
        (requirement,),
        snapshot,
        (("AdmissionOther", True),),
    )
