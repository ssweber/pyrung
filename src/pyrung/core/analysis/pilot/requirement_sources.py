"""Follow exact same-scan occurrence sources for inert repair requirements.

This module owns the strictly decreasing source walk from a failed guard read
through exact earlier writes and their enabling reads. It returns detached
requirement evidence; it never selects an obligation, chooses a repair, or
executes a scan.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from pyrung.core.analysis.causal._rung_writes import (
    RungRead,
    RungWrite,
    ScanRungWriteProjection,
    StaticRungAddress,
)
from pyrung.core.analysis.pilot.effects import occurrence_snapshot
from pyrung.core.analysis.pilot.guard_evaluation import (
    _evaluate_enabling_path_complement,
    _evaluate_run_guard_complement,
)
from pyrung.core.analysis.pilot.requirements import (
    GuardLogic,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OccurrenceSourceLink,
    RequirementSourceWalk,
    RequirementSourceWalkStatus,
)
from pyrung.core.crossing import Cmp
from pyrung.core.executor import InstructionRun, LoopIterationRun, RungRun
from pyrung.core.instruction.advance import constraint_holds


def _guard_atoms(
    condition: GuardRequirementCondition,
) -> tuple[GuardRequirementAtom, ...]:
    if isinstance(condition, GuardRequirementAtom):
        return (condition,)
    return tuple(atom for term in condition.terms for atom in _guard_atoms(term))


def _bind_guard_demanding_rung(
    condition: GuardRequirementCondition,
    rung: Any,
) -> GuardRequirementCondition:
    if isinstance(condition, GuardRequirementAtom):
        return replace(condition, demanding_rung=rung)
    return replace(
        condition,
        terms=tuple(_bind_guard_demanding_rung(term, rung) for term in condition.terms),
    )


def _residualize_guard_requirement(
    condition: GuardRequirementCondition,
    preserved_values: tuple[tuple[str, Any], ...],
    *,
    preserve_incomplete: bool = False,
) -> GuardRequirementCondition | None:
    """Remove only alternatives disproved by values the effect must preserve."""

    if isinstance(condition, GuardRequirementAtom):
        return (
            None
            if constraint_holds(condition.condition, dict(preserved_values)) is False
            else condition
        )
    filtered = tuple(
        member
        for term in condition.terms
        if (
            member := _residualize_guard_requirement(
                term,
                preserved_values,
                preserve_incomplete=preserve_incomplete,
            )
        )
        is not None
    )
    if len(filtered) == len(condition.terms):
        return condition
    if condition.logic is GuardLogic.ALL or not filtered:
        return None
    if len(filtered) == 1 and (condition.exhaustive or not preserve_incomplete):
        return filtered[0]
    return replace(condition, terms=filtered, exhaustive=False)


def _refine_preserved_tag_deadlines(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    preserved_values: tuple[tuple[str, Any], ...],
) -> GuardRequirementCondition:
    """Preserve the established protected-tag production refinement.

    The complete Stage-2 occurrence walk is exposed separately as report-only
    evidence. This compatibility seam deliberately retains the pre-Stage-2
    exact-run complement, fallback, and identity behavior used by production.
    """

    protected = dict(preserved_values)

    def refine(
        item: GuardRequirementCondition,
        *,
        remaining_hops: int,
        visited: frozenset[tuple[int, int, str, int]],
    ) -> GuardRequirementCondition:
        if isinstance(item, GuardRequirementExpr):
            terms = tuple(
                refine(term, remaining_hops=remaining_hops, visited=visited) for term in item.terms
            )
            return item if terms == item.terms else replace(item, terms=terms)
        if remaining_hops <= 0 or not isinstance(item.condition, Cmp):
            return item
        constraint = item.condition
        if constraint.bound_is_tag or constraint.tag not in protected:
            return item
        protected_view = dict(protected)
        if constraint_holds(constraint, protected_view) is not True:
            return item

        reads = tuple(
            read for read in projection.reads if occurrence_snapshot(read) == item.deadline
        )
        if len(reads) != 1:
            return item
        read = reads[0]
        observed_view = {**protected_view, constraint.tag: read.occurrence.value}
        if constraint_holds(constraint, observed_view) is not False:
            return item
        transition = projection.transition_observed_by_read(read)
        if transition is None or transition.occurrence_ordinal is None:
            return item
        definitions = tuple(
            write
            for write in projection.writes
            if write.scan_id == read.scan_id
            and write.ordinal == transition.occurrence_ordinal
            and write.transition.tag_name == constraint.tag
            and write.transition.from_value == transition.from_value
            and write.transition.to_value == transition.to_value
        )
        if len(definitions) != 1:
            return item
        definition = definitions[0]
        if not definition.run.enabled or not definition.ordinal < read.ordinal:
            return item
        before_view = {**protected_view, constraint.tag: definition.transition.from_value}
        after_view = {**protected_view, constraint.tag: definition.transition.to_value}
        if (
            constraint_holds(constraint, before_view) is not True
            or constraint_holds(constraint, after_view) is not False
        ):
            return item

        visit = (read.scan_id, read.ordinal, constraint.tag, definition.ordinal)
        if visit in visited:
            return item
        evaluation = _evaluate_run_guard_complement(definition.run, projection)
        if (
            not evaluation.value
            or not evaluation.exact
            or evaluation.requirement is None
            or not evaluation.supporting_reads
        ):
            return item
        substitute = _residualize_guard_requirement(
            evaluation.requirement,
            preserved_values,
        )
        if substitute is None:
            return item
        substitute = _bind_guard_demanding_rung(substitute, definition.run.rung)
        substitute_atoms = _guard_atoms(substitute)
        if not substitute_atoms or any(
            atom.deadline.scan_id != read.scan_id or atom.deadline.ordinal >= read.ordinal
            for atom in substitute_atoms
        ):
            return item
        return refine(
            substitute,
            remaining_hops=remaining_hops - 1,
            visited=visited | {visit},
        )

    return refine(
        condition,
        remaining_hops=len(projection.writes),
        visited=frozenset(),
    )


def derive_occurrence_source_requirement(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...] = (),
) -> RequirementSourceWalk:
    """Follow false reads through exact earlier same-scan program writes.

    The projection's live occurrence objects are the oracle.  The returned
    condition is inert evidence; this function never compiles or applies an
    assignment, chooses an alternative, or executes another scan.
    """

    return _derive_occurrence_source_requirement(
        condition,
        projection,
        preserved_values=preserved_values,
    )


def _derive_occurrence_source_requirement(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...],
) -> RequirementSourceWalk:
    result = _walk_requirement_sources(
        condition,
        projection,
        preserved_values=preserved_values,
        ceiling=None,
        remaining_hops=len(projection.writes),
        visited=frozenset(),
    )
    normalized = _normalize_guard_requirement(result.condition)
    return result if normalized is result.condition else replace(result, condition=normalized)


def _walk_requirement_sources(
    condition: GuardRequirementCondition,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...],
    ceiling: int | None,
    remaining_hops: int,
    visited: frozenset[tuple[int, int]],
) -> RequirementSourceWalk:
    if isinstance(condition, GuardRequirementExpr):
        terms: list[GuardRequirementCondition] = []
        links: list[OccurrenceSourceLink] = []
        details: list[str] = []
        complete = True
        for term in condition.terms:
            result = _walk_requirement_sources(
                term,
                projection,
                preserved_values=preserved_values,
                ceiling=ceiling,
                remaining_hops=remaining_hops,
                visited=visited,
            )
            terms.append(result.condition)
            _extend_unique(links, result.links)
            if result.status is RequirementSourceWalkStatus.INCOMPLETE:
                complete = False
                if result.detail:
                    details.append(result.detail)
        rewritten = (
            condition if tuple(terms) == condition.terms else replace(condition, terms=tuple(terms))
        )
        return RequirementSourceWalk(
            RequirementSourceWalkStatus.COMPLETE
            if complete
            else RequirementSourceWalkStatus.INCOMPLETE,
            rewritten,
            tuple(links),
            "; ".join(dict.fromkeys(details)),
        )
    return _walk_requirement_atom(
        condition,
        projection,
        preserved_values=preserved_values,
        ceiling=ceiling,
        remaining_hops=remaining_hops,
        visited=visited,
    )


def _walk_requirement_atom(
    atom: GuardRequirementAtom,
    projection: ScanRungWriteProjection,
    *,
    preserved_values: tuple[tuple[str, Any], ...],
    ceiling: int | None,
    remaining_hops: int,
    visited: frozenset[tuple[int, int]],
) -> RequirementSourceWalk:
    def incomplete(detail: str) -> RequirementSourceWalk:
        return RequirementSourceWalk(
            RequirementSourceWalkStatus.INCOMPLETE,
            atom,
            detail=detail,
        )

    if atom.deadline.scan_id != projection.scan_id:
        return incomplete("requirement deadline belongs to a different projection scan")
    reads = tuple(read for read in projection.reads if occurrence_snapshot(read) == atom.deadline)
    if len(reads) != 1:
        return incomplete("requirement deadline read is unavailable or ambiguous")
    read = reads[0]
    condition_tag = getattr(atom.condition, "tag", None)
    if (
        read.occurrence.domain != "tag"
        or not isinstance(condition_tag, str)
        or condition_tag != read.occurrence.name
    ):
        return incomplete("requirement constraint does not match its exact tag read")
    if ceiling is not None and read.ordinal >= ceiling:
        return incomplete("requirement deadline does not strictly precede its source ceiling")

    from pyrung.core.executor import WriteOccurrence

    source = read.occurrence.source
    if not isinstance(source, WriteOccurrence):
        if source == "pending":
            return incomplete("requirement read has an unjournaled pending source")
        return RequirementSourceWalk(RequirementSourceWalkStatus.COMPLETE, atom)
    if remaining_hops <= 0:
        return incomplete("occurrence-source walk exhausted its exact write bound")
    definitions = tuple(write for write in projection.writes if write.occurrence is source)
    if len(definitions) != 1:
        return incomplete("requirement read source write is unavailable or ambiguous")
    definition = definitions[0]
    if (
        source.domain != "tag"
        or source.name != read.occurrence.name
        or source.after != read.occurrence.value
        or definition.scan_id != read.scan_id
        or definition.transition.tag_name != read.occurrence.name
        or definition.transition.to_value != read.occurrence.value
    ):
        return incomplete("requirement read carries inconsistent source-write evidence")
    if not definition.run.enabled or definition.ordinal >= read.ordinal:
        return incomplete("requirement source write is disabled or not strictly earlier")
    if ceiling is not None and definition.ordinal >= ceiling:
        return incomplete("requirement source write does not strictly decrease")
    visit = (id(read.occurrence), id(source))
    if visit in visited:
        return incomplete("occurrence-source walk repeated an exact source link")
    if not isinstance(atom.condition, Cmp) or atom.condition.bound_is_tag:
        return incomplete("same-scan sourced requirement is not an exact scalar constraint")
    protected = dict(preserved_values)
    if atom.condition.tag in protected and constraint_holds(atom.condition, protected) is not True:
        # A terminal effect may require this channel to retain a value which
        # disproves the guard atom.  Preventing an earlier writer cannot make
        # that self-conflicting arm a compatible way to preserve the effect.
        return RequirementSourceWalk(RequirementSourceWalkStatus.COMPLETE, atom)
    before = {atom.condition.tag: definition.transition.from_value}
    after = {atom.condition.tag: definition.transition.to_value}
    if (
        constraint_holds(atom.condition, before) is not True
        or constraint_holds(atom.condition, after) is not False
    ):
        return incomplete("source write does not exactly cross the required scalar truth")

    evaluation = _evaluate_enabling_path_complement(
        definition.run,
        projection,
        preserve_nested_false=True,
    )
    if (
        not evaluation.value
        or not evaluation.exact
        or evaluation.requirement is None
        or not evaluation.supporting_reads
    ):
        return incomplete("source writer enabling path has no exact complement")
    substitute = _residualize_guard_requirement(
        evaluation.requirement,
        preserved_values,
        preserve_incomplete=True,
    )
    if substitute is None:
        return incomplete("source writer guard depends only on excluded preserved state")
    substitute = _bind_guard_demanding_rung(substitute, definition.run.rung)
    substitute_atoms = _guard_atoms(substitute)
    if not substitute_atoms:
        return incomplete("source writer complement has no exact scalar atom")
    if any(
        candidate.deadline.scan_id != read.scan_id
        or candidate.deadline.ordinal >= definition.ordinal
        for candidate in substitute_atoms
    ):
        return incomplete("source writer complement has a non-decreasing deadline")
    required_address = _static_run_address(projection, read.run)
    required_instruction_path = _read_instruction_path(read)
    source_address = _static_run_address(projection, definition.run)
    instruction_path = _instruction_occurrence_path(definition)
    if (
        required_address is None
        or required_instruction_path is None
        or source_address is None
        or instruction_path is None
    ):
        return incomplete("source writer dynamic branch or instruction identity is unavailable")
    link = OccurrenceSourceLink(
        required_read=occurrence_snapshot(read),
        source_write=occurrence_snapshot(definition),
        enabling_reads=tuple(occurrence_snapshot(item) for item in evaluation.supporting_reads),
        required_address=required_address,
        required_instruction_path=required_instruction_path,
        source_address=source_address,
        instruction_path=instruction_path,
    )
    substitute = _prepend_source_link(substitute, (*atom.source_links, link))
    nested = _walk_requirement_sources(
        substitute,
        projection,
        preserved_values=preserved_values,
        ceiling=definition.ordinal,
        remaining_hops=remaining_hops - 1,
        visited=visited | {visit},
    )
    links = [link]
    _extend_unique(links, nested.links)
    return replace(nested, links=tuple(links))


def _prepend_source_link(
    condition: GuardRequirementCondition,
    prefix: tuple[OccurrenceSourceLink, ...],
) -> GuardRequirementCondition:
    if isinstance(condition, GuardRequirementAtom):
        return replace(condition, source_links=(*prefix, *condition.source_links))
    return replace(
        condition,
        terms=tuple(_prepend_source_link(term, prefix) for term in condition.terms),
    )


def _static_run_address(
    projection: ScanRungWriteProjection,
    run: RungRun,
) -> StaticRungAddress | None:
    """Detach the static branch path of one exact dynamic run."""

    def find_branch(root: Any, selected: Any, path: tuple[int, ...] = ()) -> tuple[int, ...] | None:
        if root is selected:
            return path
        for index, branch in enumerate(getattr(root, "_branches", ())):
            found = find_branch(branch, selected, (*path, index))
            if found is not None:
                return found
        return None

    candidates = tuple(
        candidate
        for candidate in projection.runs
        if candidate.rung_id == run.rung_id and candidate.kind != "branch"
    )
    paths = tuple(
        path
        for candidate in candidates
        if (path := find_branch(candidate.rung, run.rung)) is not None
    )
    unique = tuple(dict.fromkeys(paths))
    if len(unique) != 1:
        return None
    return (run.rung_id.subroutine, run.rung_id.rung_index, unique[0])


def _instruction_run_path(
    run: RungRun,
    selected: InstructionRun,
) -> tuple[int, ...] | None:
    """Return the exact journal path to one projection-owned instruction."""

    def find(body: tuple[Any, ...], prefix: tuple[int, ...]) -> tuple[int, ...] | None:
        for index, item in enumerate(body):
            path = (*prefix, index)
            if item is selected:
                return path
            if isinstance(item, InstructionRun | LoopIterationRun):
                nested = find(item.body, path)
                if nested is not None:
                    return nested
        return None

    return find(run.body, ())


def _read_instruction_path(read: RungRead) -> tuple[int, ...] | None:
    """Detach a required read's exact instruction owner, or rung guard root."""

    if read.instruction is None:
        return ()
    return _instruction_run_path(read.run, read.instruction)


def _instruction_occurrence_path(write: RungWrite) -> tuple[int, ...] | None:
    """Return the exact journal path to a source write's InstructionRun."""

    if write.instruction is None:
        return None
    return _instruction_run_path(write.run, write.instruction)


def _normalize_guard_requirement(
    condition: GuardRequirementCondition,
) -> GuardRequirementCondition:
    """Flatten only identical Boolean structure without choosing an arm."""

    if isinstance(condition, GuardRequirementAtom):
        return condition
    terms: list[GuardRequirementCondition] = []
    exhaustive = condition.exhaustive
    for term in condition.terms:
        normalized = _normalize_guard_requirement(term)
        if isinstance(normalized, GuardRequirementExpr) and normalized.logic is condition.logic:
            terms.extend(normalized.terms)
            exhaustive = exhaustive and normalized.exhaustive
        else:
            terms.append(normalized)
    unique: list[GuardRequirementCondition] = []
    for term in terms:
        if not any(term == existing for existing in unique):
            unique.append(term)
    if len(unique) == 1 and exhaustive:
        return unique[0]
    rewritten = tuple(unique)
    if rewritten == condition.terms and exhaustive is condition.exhaustive:
        return condition
    return GuardRequirementExpr(condition.logic, rewritten, exhaustive=exhaustive)


def _extend_unique(
    target: list[OccurrenceSourceLink],
    additions: tuple[OccurrenceSourceLink, ...],
) -> None:
    for addition in additions:
        if addition not in target:
            target.append(addition)
