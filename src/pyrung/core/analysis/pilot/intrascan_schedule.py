"""Pure finite scheduling for exact temporal requirements.

The functions in this module only normalize Boolean alternatives and compile
compatible scalar requirements to executable :class:`PilotRung` records. They
do not execute a PLC, choose a production action, or retain search state.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any, cast

from pyrung.core.analysis.pilot.overlay import PilotRung
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementCondition,
    RequirementPhase,
    RequirementStatus,
)
from pyrung.core.analysis.pilot.world_key import _rung_identity
from pyrung.core.crossing import Cmp
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
    # Live requirements from which Boolean branch lowering produced
    # ``requirements``. The lowered requirements are exact VERIFY inputs;
    # these sources are the lifecycle obligations an accepted phase may
    # discharge. Ordinary scalar compilation has a one-to-one mapping and
    # therefore leaves this empty.
    requirement_sources: tuple[ActiveRequirement, ...] = ()
    # Exact branch-lowered leaves grouped under their lifecycle parent. This
    # lets correction composition name ownership without asking a Boolean
    # parent expression for a scalar destination.
    requirement_bindings: tuple[tuple[ActiveRequirement, tuple[ActiveRequirement, ...]], ...] = ()

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.source_world_key,
            self.checkpoint_owner,
            self.phase,
            tuple(requirement.identity for requirement in self.requirements),
            tuple(requirement.identity for requirement in self.requirement_sources),
            self.assignments,
            tuple(_rung_identity(rung) for rung in self.pilot_rungs),
        )


@dataclass(frozen=True)
class ScheduleCompilation:
    """Fail-closed compilation result."""

    schedule: RequirementSchedule | None = None
    detail: str = ""


def iter_guard_alternatives(
    condition: GuardRequirementCondition,
) -> Iterator[tuple[GuardRequirementAtom, ...]]:
    """Yield exact DNF alternatives without eagerly expanding the whole tree."""

    if isinstance(condition, GuardRequirementAtom):
        yield (condition,)
        return
    if condition.logic is GuardLogic.ANY:
        for term in condition.terms:
            yield from iter_guard_alternatives(term)
        return
    if condition.logic is not GuardLogic.ALL:
        return

    def combine(
        index: int,
        prefix: tuple[GuardRequirementAtom, ...],
    ) -> Iterator[tuple[GuardRequirementAtom, ...]]:
        if index == len(condition.terms):
            yield prefix
            return
        for branch in iter_guard_alternatives(condition.terms[index]):
            yield from combine(index + 1, (*prefix, *branch))

    yield from combine(0, ())


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


def satisfying_values(
    tag: Tag,
    constraints: tuple[Cmp, ...],
    snapshot: dict[str, Any],
) -> tuple[Any, ...]:
    """Return deterministic finite representatives satisfying all constraints."""

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

    def rank(value: Any) -> tuple[Any, ...]:
        if isinstance(value, int | float) and isinstance(current, int | float):
            return (0, abs(value - current), value)
        return (1, repr(value))

    return tuple(sorted(valid, key=rank))


def _satisfying_value(
    tag: Tag,
    constraints: tuple[Cmp, ...],
    snapshot: dict[str, Any],
) -> Any | None:
    values = satisfying_values(tag, constraints, snapshot)
    return values[0] if values else None


def compile_scalar_schedule(
    requirements: tuple[ActiveRequirement, ...],
    plc: Any,
    *,
    guard: Any,
    causal_anchor: tuple[Any, Any] | None = None,
    allow_deferred_authoritative: bool = False,
) -> ScheduleCompilation:
    """Compile compatible adjustable scalars at one executable source.

    Ordinary closure requires every requirement to carry that same exact
    source. A working theory may instead supply its explicitly restored causal
    anchor: accumulated requirements retain the distinct diagnostic owners
    that taught them to us, while the schedule is placed at the selected root.
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
    anchor_owner, anchor_key = (
        (first.checkpoint_owner, first.source_world_key) if causal_anchor is None else causal_anchor
    )
    if any(requirement.phase is not first.phase for requirement in scalar):
        return ScheduleCompilation(detail="requirements do not share one schedule phase")
    if causal_anchor is None and any(
        requirement.checkpoint_owner is not first.checkpoint_owner
        or requirement.source_world_key != first.source_world_key
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
            if allow_deferred_authoritative:
                continue
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
            requirements=tuple(assignable) if allow_deferred_authoritative else scalar,
            assignments=tuple(assignments),
            pilot_rungs=pilot_rungs,
            checkpoint_owner=anchor_owner,
            source_world_key=anchor_key,
            phase=first.phase,
        )
    )
