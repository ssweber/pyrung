"""Compile exact active requirements into one bounded local repair.

This module is the Phase-5 integration owner.  It does not orient the outer
drive, retain an action suffix, or broaden requirement lifetime.  It compiles
only compatible ACTIVE/STEADY scalar assignments and describes exact guard
alternatives which the caller may satisfy by nesting ordinary current-source
work into the already selected local transaction.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from itertools import product
from typing import Any, cast

from pyrung.core.analysis.pilot.overlay import PilotRung
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
from pyrung.core.analysis.pilot.world_key import _rung_identity
from pyrung.core.crossing import Cmp, Constraint
from pyrung.core.fold import _extract_condition_reads
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.tag import Tag, TagType


@dataclass(frozen=True)
class RequirementSchedule:
    """One simultaneous assignment phase at one exact causal source."""

    requirements: tuple[ActiveRequirement, ...]
    assignments: tuple[tuple[str, Any], ...]
    pilot_rungs: tuple[PilotRung, ...]
    checkpoint_owner: Any
    source_world_key: Any
    phase: RequirementPhase

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.source_world_key,
            self.checkpoint_owner,
            self.phase,
            tuple(requirement.identity for requirement in self.requirements),
            self.assignments,
            tuple(_rung_identity(rung) for rung in self.pilot_rungs),
        )


@dataclass(frozen=True)
class ScheduleCompilation:
    """Fail-closed compilation result."""

    schedule: RequirementSchedule | None = None
    detail: str = ""


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
    landing = dict(snapshot)
    landing.update(actions)
    return not active_requirement_violations(requirements, snapshot, landing)


def guard_alternatives(
    condition: GuardRequirementCondition,
) -> tuple[tuple[GuardRequirementAtom, ...], ...]:
    """Return exact DNF alternatives without flattening an OR into an AND."""

    if isinstance(condition, GuardRequirementAtom):
        return ((condition,),)
    terms = tuple(guard_alternatives(term) for term in condition.terms)
    if condition.logic is GuardLogic.ANY:
        return tuple(branch for alternatives in terms for branch in alternatives)
    if condition.logic is GuardLogic.ALL:
        return tuple(
            tuple(atom for branch in branches for atom in branch) for branches in product(*terms)
        )
    return ()


def _tag_limits(tag: Tag) -> tuple[int | float | None, int | float | None]:
    limits: dict[TagType, tuple[int | float | None, int | float | None]] = {
        TagType.INT: (-32768, 32767),
        TagType.DINT: (-2147483648, 2147483647),
        TagType.WORD: (0, 65535),
        TagType.REAL: (None, None),
    }
    lower, upper = limits.get(tag.type, (None, None))
    if tag.min is not None:
        lower = max(lower, tag.min) if lower is not None else tag.min
    if tag.max is not None:
        upper = min(upper, tag.max) if upper is not None else tag.max
    return lower, upper


def _integer_candidates(constraints: tuple[Cmp, ...], tag: Tag, current: Any) -> set[Any]:
    lower, upper = _tag_limits(tag)
    values: set[Any] = {current, tag.default}
    if lower is not None:
        values.add(math.ceil(lower))
    if upper is not None:
        values.add(math.floor(upper))
    for constraint in constraints:
        bound = constraint.bound
        if not isinstance(bound, int | float) or isinstance(bound, bool):
            continue
        if constraint.op == ">":
            values.add(math.floor(bound) + 1)
        elif constraint.op == ">=":
            values.add(math.ceil(bound))
        elif constraint.op == "<":
            values.add(math.ceil(bound) - 1)
        elif constraint.op == "<=":
            values.add(math.floor(bound))
        elif constraint.op == "==" and float(bound).is_integer():
            values.add(int(bound))
        elif constraint.op == "!=":
            values.update((math.floor(bound) - 1, math.floor(bound) + 1))
    return values


def _real_candidates(constraints: tuple[Cmp, ...], tag: Tag, current: Any) -> set[Any]:
    lower, upper = _tag_limits(tag)
    values: set[Any] = {current, tag.default}
    if lower is not None:
        values.add(float(lower))
    if upper is not None:
        values.add(float(upper))
    for constraint in constraints:
        bound = constraint.bound
        if not isinstance(bound, int | float) or isinstance(bound, bool):
            continue
        numeric = float(bound)
        if constraint.op == ">":
            values.add(math.nextafter(numeric, math.inf))
        elif constraint.op == ">=":
            values.add(numeric)
        elif constraint.op == "<":
            values.add(math.nextafter(numeric, -math.inf))
        elif constraint.op == "<=":
            values.add(numeric)
        elif constraint.op == "==":
            values.add(numeric)
        elif constraint.op == "!=":
            values.update((math.nextafter(numeric, -math.inf), math.nextafter(numeric, math.inf)))
    return values


def _satisfying_value(
    tag: Tag,
    constraints: tuple[Cmp, ...],
    snapshot: dict[str, Any],
) -> Any | None:
    current = snapshot.get(tag.name, tag.default)
    if tag.choices:
        candidates = set(tag.choices)
    elif tag.type in {TagType.INT, TagType.DINT, TagType.WORD}:
        candidates = _integer_candidates(constraints, tag, current)
    elif tag.type is TagType.REAL:
        candidates = _real_candidates(constraints, tag, current)
    else:
        candidates = {current, tag.default, False, True}
        for constraint in constraints:
            if constraint.op == "==":
                candidates.add(constraint.bound)

    lower, upper = _tag_limits(tag)
    valid: list[Any] = []
    for value in candidates:
        if lower is not None and (
            not isinstance(value, int | float) or isinstance(value, bool) or value < lower
        ):
            continue
        if upper is not None and (
            not isinstance(value, int | float) or isinstance(value, bool) or value > upper
        ):
            continue
        proposed = {**snapshot, tag.name: value}
        if all(constraint_holds(constraint, proposed) is True for constraint in constraints):
            valid.append(value)
    if not valid:
        return None

    def rank(value: Any) -> tuple[Any, ...]:
        if isinstance(value, int | float) and isinstance(current, int | float):
            return (0, abs(value - current), value)
        return (1, repr(value))

    return min(valid, key=rank)


def compile_scalar_schedule(
    requirements: tuple[ActiveRequirement, ...],
    plc: Any,
    *,
    guard: Any,
) -> ScheduleCompilation:
    """Compile one exact source's compatible adjustable scalar requirements.

    Guard requirements are intentionally omitted here: their conditions name
    program state and must be satisfied by ordinary nested trace work.  A
    configured/program-written scalar is never assigned.  It may remain as a
    constraint when already true at the source; when false, direct lowering is
    unavailable.
    """

    scalar = tuple(
        requirement for requirement in requirements if isinstance(requirement.condition, Cmp)
    )
    if not scalar:
        return ScheduleCompilation(detail="no scalar requirements to compile")
    first = scalar[0]
    if any(
        requirement.status is not RequirementStatus.ACTIVE
        or requirement.phase is not RequirementPhase.STEADY
        for requirement in scalar
    ):
        return ScheduleCompilation(detail="only ACTIVE/STEADY requirements may lower")
    if any(
        requirement.checkpoint_owner is not first.checkpoint_owner
        or requirement.source_world_key != first.source_world_key
        or requirement.phase is not first.phase
        for requirement in scalar
    ):
        return ScheduleCompilation(detail="requirements do not share one exact causal source")
    snapshot = dict(plc.state.tags)
    assignable: list[ActiveRequirement] = []
    for requirement in scalar:
        if requirement.permits_assignment:
            assignable.append(requirement)
            continue
        condition = cast(Cmp, requirement.condition)
        if constraint_holds(condition, snapshot) is not True:
            return ScheduleCompilation(
                detail="an unsatisfied authoritative operand forbids direct assignment"
            )

    by_tag: dict[str, list[Cmp]] = {}
    for requirement in assignable:
        condition = cast(Cmp, requirement.condition)
        if condition.bound_is_tag:
            return ScheduleCompilation(detail="tag-relative scalar lowering is unsupported")
        by_tag.setdefault(condition.tag, []).append(condition)

    guard_names = _extract_condition_reads(guard)
    assignments: list[tuple[str, Any]] = []
    for name, conditions in sorted(by_tag.items()):
        if name in guard_names:
            return ScheduleCompilation(detail=f"repair guard self-demands {name!r}")
        tag = plc._known_tags_by_name.get(name)
        if tag is None:
            return ScheduleCompilation(detail=f"unknown assignment destination {name!r}")
        value = _satisfying_value(tag, tuple(conditions), snapshot)
        if value is None:
            return ScheduleCompilation(detail=f"incompatible scalar requirements for {name!r}")
        assignments.append((name, value))

    pilot_rungs = tuple(PilotRung(tag, value, guard) for tag, value in assignments)
    return ScheduleCompilation(
        RequirementSchedule(
            requirements=scalar,
            assignments=tuple(assignments),
            pilot_rungs=pilot_rungs,
            checkpoint_owner=first.checkpoint_owner,
            source_world_key=first.source_world_key,
            phase=first.phase,
        )
    )
