"""Compatibility facade for active-requirement schedule consumers.

Pure schedule compilation is owned by the report-only intrascan service. This
module retains the established imports used by production recovery while also
keeping current-world requirement admission helpers local.
"""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.pilot.intrascan_schedule import (
    RequirementSchedule,
    ScheduleCompilation,
    compile_scalar_schedule,
    guard_alternatives,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.crossing import Constraint
from pyrung.core.instruction.advance import constraint_holds

__all__ = [
    "RequirementSchedule",
    "ScheduleCompilation",
    "compile_scalar_schedule",
    "guard_alternatives",
]


def requirement_condition_holds(
    condition: Constraint | GuardRequirementCondition,
    snapshot: dict[str, Any],
) -> bool | None:
    """Evaluate one retained requirement without weakening unknown shapes.

    Active requirements are navigation constraints even when their operand is
    configured or program-written and therefore has no executable PilotRung.
    This evaluator is shared by Orientation's admission read and Verify's
    landing proof so those two seams interpret compound guards identically.
    """

    if isinstance(condition, GuardRequirementAtom):
        return constraint_holds(condition.condition, snapshot)
    if isinstance(condition, GuardRequirementExpr):
        verdicts = tuple(requirement_condition_holds(term, snapshot) for term in condition.terms)
        if condition.logic is GuardLogic.ALL:
            if any(verdict is False for verdict in verdicts):
                return False
            return True if all(verdict is True for verdict in verdicts) else None
        if condition.logic is GuardLogic.ANY:
            if any(verdict is True for verdict in verdicts):
                return True
            return False if all(verdict is False for verdict in verdicts) else None
        return None
    return constraint_holds(condition, snapshot)


def active_requirement_violations(
    requirements: tuple[ActiveRequirement, ...],
    before: dict[str, Any],
    after: dict[str, Any],
) -> tuple[ActiveRequirement, ...]:
    """Return exact ACTIVE/STEADY truths a candidate would invalidate.

    An unresolved condition is not guessed into a veto.  A requirement which
    was already false is recovery work, rather than proof that an unrelated
    candidate destroyed it.  Only a proved true-to-false transition is a
    preservation violation.
    """

    return tuple(
        requirement
        for requirement in requirements
        if requirement.status is RequirementStatus.ACTIVE
        and requirement.phase is RequirementPhase.STEADY
        # Adjustable operands are physically held by their PilotRungs. Guard
        # prerequisites are occurrence-scoped: they may be true at their
        # demanding read and legitimately false at the settled landing (for
        # example State==1 enabling the transition to State==2). Endpoint
        # preservation is therefore the missing constraint specifically for
        # authoritative operands which recovery must honor but cannot assign.
        and getattr(requirement, "operand_authority", None)
        in {OperandAuthority.CONFIGURED, OperandAuthority.PROGRAM_WRITTEN}
        and requirement_condition_holds(requirement.condition, before) is True
        and requirement_condition_holds(requirement.condition, after) is False
    )


def actions_preserve_active_requirements(
    requirements: tuple[ActiveRequirement, ...],
    snapshot: dict[str, Any],
    actions: tuple[tuple[str, Any], ...],
) -> bool:
    """Whether an atomic candidate overlay preserves every proved live fact."""

    if not actions or not requirements:
        return True
    assigned_tags = {tag for tag, _value in actions}
    for requirement in requirements:
        if requirement.status is not RequirementStatus.ACTIVE:
            continue
        condition = requirement.condition
        if not isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
            continue
        if any(
            not atom.permits_assignment and getattr(atom.condition, "tag", None) in assigned_tags
            for alternative in guard_alternatives(condition)
            for atom in alternative
        ):
            return False
    landing = dict(snapshot)
    landing.update(actions)
    return not active_requirement_violations(requirements, snapshot, landing)
