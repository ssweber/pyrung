"""Evaluate exact dynamic rung guards from execution projections.

This module reconstructs guard truth and scalar complements from one recorded
projection. It returns detached guard conditions and supporting reads; it does
not mint ActiveRequirements, select repairs, or own Pilot state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.causal._rung_writes import RungRead, ScanRungWriteProjection
from pyrung.core.analysis.pilot.effects import occurrence_snapshot
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
)
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    IntTruthyCondition,
    NormallyClosedCondition,
)
from pyrung.core.crossing import Cmp, Constraint, complement_scalar_constraint
from pyrung.core.executor import RungRun
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.tag import ImmediateRef, Tag


@dataclass(frozen=True)
class _GuardEvaluation:
    value: bool
    supporting_reads: tuple[RungRead, ...]
    requirement: GuardRequirementCondition | None = None
    exact: bool = True


class _GuardReadCursor:
    """Match a replayed static guard evaluation to its exact journal reads."""

    def __init__(self, reads: tuple[RungRead, ...], view: Any) -> None:
        self._reads = reads
        self._view = view
        self._cursor = 0

    def evaluate_leaf(self, condition: Condition) -> tuple[bool, tuple[RungRead, ...]] | None:
        captured: list[tuple[str, str, Any, Any]] = []

        def _capture(domain: str, name: str, value: Any, origin: Any, source: Any) -> None:
            captured.append((domain, name, value, source if source is not None else origin))

        previous_sink = self._view._read_sink
        self._view._read_sink = _capture
        try:
            value = condition.evaluate(self._view)
        finally:
            self._view._read_sink = previous_sink

        matched: list[RungRead] = []
        for domain, name, value_read, source in captured:
            if self._cursor >= len(self._reads):
                return None
            exact = self._reads[self._cursor]
            occurrence = exact.occurrence
            same_value = occurrence.value is value_read
            if not same_value:
                try:
                    same_value = occurrence.value == value_read
                except (TypeError, ValueError):
                    same_value = False
            exact_source = occurrence.source
            same_source = (
                exact_source == source
                if isinstance(exact_source, str) and isinstance(source, str)
                else exact_source is source
            )
            if (
                occurrence.domain != domain
                or occurrence.name != name
                or same_value is not True
                or not same_source
            ):
                return None
            matched.append(exact)
            self._cursor += 1
        return bool(value), tuple(matched)


def _tag_name(value: Any) -> str | None:
    if isinstance(value, ImmediateRef):
        value = value.value
    return value.name if isinstance(value, Tag) else None


_GUARD_COMPARE_OPERATORS: dict[type[Condition], str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
}


def _guard_leaf_constraint(condition: Condition) -> Constraint | None:
    """Translate only exact scalar guard leaves into neutral constraints."""

    if isinstance(condition, BitCondition):
        tag = _tag_name(condition.tag)
        return Cmp(tag, "==", True) if tag is not None else None
    if isinstance(condition, NormallyClosedCondition):
        tag = _tag_name(condition.tag)
        return Cmp(tag, "==", False) if tag is not None else None
    if isinstance(condition, IntTruthyCondition):
        return Cmp(condition.tag.name, "!=", 0)
    operator = _GUARD_COMPARE_OPERATORS.get(type(condition))
    if operator is None:
        return None
    tag = _tag_name(getattr(condition, "tag", None))
    if tag is None:
        return None
    bound = getattr(condition, "value", None)
    bound_tag = _tag_name(bound)
    if bound_tag is not None:
        return Cmp(tag, operator, bound_tag, bound_is_tag=True)
    if isinstance(bound, Tag | ImmediateRef) or hasattr(bound, "evaluate"):
        return None
    return Cmp(tag, operator, bound)


def _evaluate_guard_condition(
    condition: Condition,
    cursor: _GuardReadCursor,
    path: tuple[int, ...],
) -> _GuardEvaluation:
    """Reduce one observed false condition to its exact enabling frontier."""

    if isinstance(condition, AllCondition):
        supporting: list[RungRead] = []
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_condition(child, cursor, (*path, index))
            supporting.extend(result.supporting_reads)
            if not result.exact:
                return _GuardEvaluation(False, tuple(supporting), exact=False)
            if not result.value:
                # Short-circuit semantics make the first false AND child the
                # exact narrower condition. Unobserved suffixes are not minted.
                return _GuardEvaluation(
                    False,
                    tuple(supporting),
                    requirement=result.requirement,
                )
        return _GuardEvaluation(True, tuple(supporting))

    if isinstance(condition, AnyCondition):
        supporting = []
        alternatives: list[GuardRequirementCondition] = []
        exhaustive = True
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_condition(child, cursor, (*path, index))
            supporting.extend(result.supporting_reads)
            if result.value:
                return _GuardEvaluation(True, tuple(supporting))
            if not result.exact or result.requirement is None:
                # The dynamic rung proves the pure OR was false. An opaque arm
                # prevents an exhaustive inverse, but any separately exact arm
                # remains a sufficient, sound way to make the OR true.
                exhaustive = False
                continue
            alternatives.append(result.requirement)
        if not alternatives:
            return _GuardEvaluation(False, tuple(supporting), exact=False)
        requirement: GuardRequirementCondition
        if len(alternatives) == 1 and exhaustive:
            requirement = alternatives[0]
        else:
            requirement = GuardRequirementExpr(
                GuardLogic.ANY,
                tuple(alternatives),
                exhaustive=exhaustive,
            )
        return _GuardEvaluation(False, tuple(supporting), requirement=requirement)

    captured = cursor.evaluate_leaf(condition)
    if captured is None:
        return _GuardEvaluation(False, (), exact=False)
    value, reads = captured
    if value:
        return _GuardEvaluation(True, reads)
    constraint = _guard_leaf_constraint(condition)
    if constraint is None or not reads:
        return _GuardEvaluation(False, reads, exact=False)
    occurrences = tuple(occurrence_snapshot(read) for read in reads)
    return _GuardEvaluation(
        False,
        reads,
        requirement=GuardRequirementAtom(
            condition=constraint,
            supporting_occurrences=occurrences,
            deadline=occurrences[-1],
            source_path=path,
        ),
    )


def _evaluate_run_guard(
    run: RungRun,
    projection: ScanRungWriteProjection,
) -> _GuardEvaluation:
    conditions = tuple(getattr(run.rung, "_conditions", ()))
    branch_start = getattr(run.rung, "_branch_condition_start", 0)
    if run.kind == "branch" or branch_start:
        conditions = conditions[branch_start:]
    cursor = _GuardReadCursor(projection.reads_for_run(run), run.view)
    supporting: list[RungRead] = []
    for index, condition in enumerate(conditions):
        result = _evaluate_guard_condition(condition, cursor, (index,))
        supporting.extend(result.supporting_reads)
        if not result.exact:
            return _GuardEvaluation(False, tuple(supporting), exact=False)
        if not result.value:
            return _GuardEvaluation(
                False,
                tuple(supporting),
                requirement=result.requirement,
            )
    return _GuardEvaluation(True, tuple(supporting))


def _evaluate_guard_complement(
    condition: Condition,
    cursor: _GuardReadCursor,
    path: tuple[int, ...],
    *,
    preserve_nested_false: bool = False,
) -> _GuardEvaluation:
    """Invert one exactly observed true guard without guessing unread arms."""

    if isinstance(condition, AllCondition):
        supporting: list[RungRead] = []
        alternatives: list[GuardRequirementCondition] = []
        exhaustive = True
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_complement(
                child,
                cursor,
                (*path, index),
                preserve_nested_false=preserve_nested_false,
            )
            supporting.extend(result.supporting_reads)
            if not result.exact:
                exhaustive = False
                continue
            if not result.value:
                if not preserve_nested_false:
                    return _GuardEvaluation(False, tuple(supporting), exact=True)
                # A false nested AND already keeps an enclosing OR disabled.
                # Every earlier observed-true child can also be made false;
                # retain those alternatives as well as this exact false
                # frontier. An unread suffix makes the list non-exhaustive.
                if result.requirement is not None:
                    alternatives.append(result.requirement)
                exhaustive = exhaustive and index == len(condition.conditions) - 1
                if not alternatives:
                    return _GuardEvaluation(False, tuple(supporting), exact=False)
                requirement = (
                    alternatives[0]
                    if len(alternatives) == 1 and exhaustive
                    else GuardRequirementExpr(
                        GuardLogic.ANY,
                        tuple(alternatives),
                        exhaustive=exhaustive,
                    )
                )
                return _GuardEvaluation(
                    False,
                    tuple(supporting),
                    requirement=requirement,
                )
            if result.requirement is not None:
                alternatives.append(result.requirement)
        if not alternatives:
            return _GuardEvaluation(True, tuple(supporting), exact=False)
        requirement = (
            alternatives[0]
            if len(alternatives) == 1 and exhaustive
            else GuardRequirementExpr(
                GuardLogic.ANY,
                tuple(alternatives),
                exhaustive=exhaustive,
            )
        )
        return _GuardEvaluation(True, tuple(supporting), requirement=requirement)

    if isinstance(condition, AnyCondition):
        supporting: list[RungRead] = []
        conjuncts: list[GuardRequirementCondition] = []
        any_true = False
        for index, child in enumerate(condition.conditions):
            result = _evaluate_guard_complement(
                child,
                cursor,
                (*path, index),
                preserve_nested_false=preserve_nested_false,
            )
            supporting.extend(result.supporting_reads)
            if not result.exact or result.requirement is None:
                # A true OR short-circuits.  Its unread suffix cannot be
                # asserted false merely because the first observed arm was
                # true, so the exact dual is unavailable.
                return _GuardEvaluation(any_true, tuple(supporting), exact=False)
            any_true = any_true or result.value
            conjuncts.append(result.requirement)
        if not any_true:
            if not preserve_nested_false:
                return _GuardEvaluation(False, tuple(supporting), exact=True)
            if not conjuncts:
                return _GuardEvaluation(False, tuple(supporting), exact=False)
            requirement = (
                conjuncts[0]
                if len(conjuncts) == 1
                else GuardRequirementExpr(GuardLogic.ALL, tuple(conjuncts))
            )
            return _GuardEvaluation(
                False,
                tuple(supporting),
                requirement=requirement,
            )
        requirement = (
            conjuncts[0]
            if len(conjuncts) == 1
            else GuardRequirementExpr(GuardLogic.ALL, tuple(conjuncts))
        )
        return _GuardEvaluation(True, tuple(supporting), requirement=requirement)

    captured = cursor.evaluate_leaf(condition)
    if captured is None:
        return _GuardEvaluation(False, (), exact=False)
    value, reads = captured
    constraint = _guard_leaf_constraint(condition)
    complement = complement_scalar_constraint(constraint) if constraint is not None else None
    if complement is None or not reads:
        return _GuardEvaluation(value, reads, exact=False)
    occurrences = tuple(occurrence_snapshot(read) for read in reads)
    return _GuardEvaluation(
        value,
        reads,
        requirement=GuardRequirementAtom(
            condition=complement,
            supporting_occurrences=occurrences,
            deadline=occurrences[-1],
            source_path=path,
        ),
    )


def _evaluate_run_guard_complement(
    run: RungRun,
    projection: ScanRungWriteProjection,
    *,
    preserve_nested_false: bool = False,
) -> _GuardEvaluation:
    """Complement the implicit conjunction which enabled one exact run."""

    conditions = tuple(getattr(run.rung, "_conditions", ()))
    branch_start = getattr(run.rung, "_branch_condition_start", 0)
    if run.kind == "branch" or branch_start:
        conditions = conditions[branch_start:]
    cursor = _GuardReadCursor(projection.reads_for_run(run), run.view)
    supporting: list[RungRead] = []
    alternatives: list[GuardRequirementCondition] = []
    exhaustive = True
    for index, condition in enumerate(conditions):
        result = _evaluate_guard_complement(
            condition,
            cursor,
            (index,),
            preserve_nested_false=preserve_nested_false,
        )
        supporting.extend(result.supporting_reads)
        if not result.exact:
            exhaustive = False
            continue
        if not result.value:
            return _GuardEvaluation(False, tuple(supporting), exact=True)
        if result.requirement is not None:
            alternatives.append(result.requirement)
    if not alternatives:
        return _GuardEvaluation(True, tuple(supporting), exact=False)
    requirement = (
        alternatives[0]
        if len(alternatives) == 1 and exhaustive
        else GuardRequirementExpr(
            GuardLogic.ANY,
            tuple(alternatives),
            exhaustive=exhaustive,
        )
    )
    return _GuardEvaluation(True, tuple(supporting), requirement=requirement)


def _evaluate_enabling_path_complement(
    selected: RungRun,
    projection: ScanRungWriteProjection,
    *,
    preserve_nested_false: bool = False,
) -> _GuardEvaluation:
    """Find exact sufficient ways to disable an enabled nested writer path."""

    ancestors = tuple(
        sorted(
            (
                candidate
                for candidate in projection.runs
                if candidate is not selected
                and candidate.enabled
                and any(nested is selected for nested in candidate.rung_occurrences)
            ),
            key=lambda candidate: candidate.depth,
            reverse=True,
        )
    )
    alternatives: list[GuardRequirementCondition] = []
    supporting: list[RungRead] = []
    exhaustive = True
    for run in (selected, *ancestors):
        conditions = tuple(getattr(run.rung, "_conditions", ()))
        branch_start = getattr(run.rung, "_branch_condition_start", 0)
        if run.kind == "branch" or branch_start:
            conditions = conditions[branch_start:]
        if not conditions:
            continue
        evaluation = _evaluate_run_guard_complement(
            run,
            projection,
            preserve_nested_false=preserve_nested_false,
        )
        if not evaluation.exact or evaluation.requirement is None:
            exhaustive = False
            for requirement, reads in _unique_scalar_guard_complements(run, projection):
                alternatives.append(requirement)
                supporting.extend(reads)
            continue
        alternatives.append(evaluation.requirement)
        supporting.extend(evaluation.supporting_reads)
    if not alternatives or not supporting:
        return _GuardEvaluation(True, tuple(supporting), exact=False)
    requirement = (
        alternatives[0]
        if len(alternatives) == 1 and exhaustive
        else GuardRequirementExpr(
            GuardLogic.ANY,
            tuple(alternatives),
            exhaustive=exhaustive,
        )
    )
    return _GuardEvaluation(True, tuple(supporting), requirement=requirement)


def _unique_scalar_guard_complements(
    run: RungRun,
    projection: ScanRungWriteProjection,
) -> tuple[tuple[GuardRequirementAtom, tuple[RungRead, ...]], ...]:
    """Recover exact scalar alternatives beside an opaque true guard term.

    Re-evaluating a composite condition can fail source-token matching after a
    short-circuited arm.  A top-level scalar leaf remains independently exact
    when each of its operands has one projection-owned read in this run.
    """

    conditions = tuple(getattr(run.rung, "_conditions", ()))
    branch_start = getattr(run.rung, "_branch_condition_start", 0)
    if run.kind == "branch" or branch_start:
        conditions = conditions[branch_start:]
    run_reads = projection.reads_for_run(run)
    result: list[tuple[GuardRequirementAtom, tuple[RungRead, ...]]] = []
    for index, condition in enumerate(conditions):
        constraint = _guard_leaf_constraint(condition)
        complement = complement_scalar_constraint(constraint) if constraint is not None else None
        if constraint is None or complement is None:
            continue
        tag = getattr(constraint, "tag", None)
        if not isinstance(tag, str):
            continue
        names = (tag,)
        if isinstance(constraint, Cmp) and constraint.bound_is_tag:
            if not isinstance(constraint.bound, str):
                continue
            names = (*names, constraint.bound)
        selected_reads: list[RungRead] = []
        snapshot: dict[str, Any] = {}
        exact = True
        for name in names:
            matches = tuple(read for read in run_reads if read.occurrence.name == name)
            if len(matches) != 1:
                exact = False
                break
            selected_reads.append(matches[0])
            snapshot[name] = matches[0].occurrence.value
        if not exact or constraint_holds(constraint, snapshot) is not True:
            continue
        selected_reads.sort(key=lambda read: read.ordinal)
        occurrences = tuple(occurrence_snapshot(read) for read in selected_reads)
        result.append(
            (
                GuardRequirementAtom(
                    condition=complement,
                    supporting_occurrences=occurrences,
                    deadline=occurrences[-1],
                    source_path=(index,),
                ),
                tuple(selected_reads),
            )
        )
    return tuple(result)
