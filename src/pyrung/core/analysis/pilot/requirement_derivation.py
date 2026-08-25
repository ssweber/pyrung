"""Derive exact repair requirements from execution evidence.

This module interprets failed effects, guard paths, overwrites, and advance
operands. It produces the inert contracts owned by
:mod:`requirements`; it never chooses or executes a repair.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import replace
from types import MappingProxyType
from typing import Any

from pyrung.core.analysis.causal._rung_writes import (
    RungRead,
    RungWrite,
    ScanRungWriteProjection,
)
from pyrung.core.analysis.pilot.advance import AdvanceIndex, AdvanceOwner
from pyrung.core.analysis.pilot.effects import (
    EffectOccurrenceSnapshot,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.guard_evaluation import (
    _evaluate_enabling_path_complement,
    _evaluate_run_guard,
)
from pyrung.core.analysis.pilot.requirement_sources import (
    _bind_guard_demanding_rung,
    _guard_atoms,
    _refine_preserved_tag_deadlines,
    _residualize_guard_requirement,
    derive_occurrence_source_requirement,
)
from pyrung.core.analysis.pilot.requirements import (
    _SWAPPED_OPERATORS,
    ActiveRequirement,
    FailureExplanation,
    FailureExplanationKind,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementDerivation,
    RequirementPhase,
    classify_guard_operand_authority,
)
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.simplified import _condition_to_expr
from pyrung.core.analysis.write_sites import instruction_writes_tag
from pyrung.core.crossing import (
    AffineCmp,
    Cmp,
    complement_scalar_constraint,
)
from pyrung.core.executor import InstructionRun, LoopIterationRun, RungRun
from pyrung.core.instruction.advance import constraint_holds


def bind_guard_operand_authorities(
    requirement: ActiveRequirement | None,
    *,
    steerable: frozenset[str],
    program_written: frozenset[str],
    configured: frozenset[str] = frozenset(),
) -> ActiveRequirement | None:
    """Attach exact per-atom ownership to one derived guard requirement."""

    if requirement is None or not isinstance(
        requirement.condition,
        GuardRequirementAtom | GuardRequirementExpr,
    ):
        return requirement

    condition = bind_guard_condition_operand_authorities(
        requirement.condition,
        steerable=steerable,
        program_written=program_written,
        configured=configured,
    )
    authorities = {atom.operand_authority for atom in _guard_atoms(condition)}
    summary = authorities.pop() if len(authorities) == 1 else OperandAuthority.UNKNOWN
    return replace(
        requirement,
        condition=condition,
        operand_authority=summary,
    )


def bind_guard_condition_operand_authorities(
    condition: GuardRequirementCondition,
    *,
    steerable: frozenset[str],
    program_written: frozenset[str],
    configured: frozenset[str] = frozenset(),
) -> GuardRequirementCondition:
    """Bind every atom in detached transitive guard evidence."""

    if isinstance(condition, GuardRequirementExpr):
        return replace(
            condition,
            terms=tuple(
                bind_guard_condition_operand_authorities(
                    term,
                    steerable=steerable,
                    program_written=program_written,
                    configured=configured,
                )
                for term in condition.terms
            ),
        )
    tag = getattr(condition.condition, "tag", None)
    authority = (
        classify_guard_operand_authority(
            tag,
            steerable=steerable,
            program_written=program_written,
            configured=configured,
        )
        if isinstance(tag, str)
        else OperandAuthority.UNKNOWN
    )
    return replace(condition, operand_authority=authority)


def _unknown(detail: str) -> RequirementDerivation:
    return RequirementDerivation(FailureExplanation(FailureExplanationKind.UNKNOWN, detail=detail))


def _literal_requirement_for_operand(
    relation: Cmp | AffineCmp,
    operand: RungRead,
    observed_operands: Mapping[str, Any],
) -> Cmp | None:
    """Transpose one exact scalar relation onto the selected operand."""

    # Affine storage width, scale, and quantization require a separately proved
    # adapter. Do not erase that information into an apparently exact Cmp.
    if not isinstance(relation, Cmp):
        return None

    operand_tag = operand.occurrence.name
    # The relation's coordinate is the *post-advance* accumulator, while its
    # sibling read is the entry accumulator. Equating those without an exact
    # delta inverse moves the requirement across time and is unsound. Phase 4
    # therefore transposes only onto a distinct, exact bound-parameter read.
    if operand_tag == relation.tag:
        return None
    if not relation.bound_is_tag or operand_tag != str(relation.bound):
        return None
    op = _SWAPPED_OPERATORS.get(relation.op)
    other = observed_operands.get(relation.tag)
    if op is None or other is None:
        return None
    return Cmp(operand_tag, op, other)


def _exact_instruction_operands(
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    owner: AdvanceOwner | None = None,
) -> Mapping[str, Any] | None:
    """Return exact owner operands, including its post-advance accumulator.

    Timer/counter execution may journal the Done write before the accumulator
    write even though completion is calculated from that next accumulator.
    ``AdvanceProfile.accumulator`` declares that semantic coordinate; only the
    exact same instruction occurrence's unique write may replace its entry read.
    """

    result: dict[str, Any] = {}
    for read in projection.reads_observed_by_write(definition):
        tag = read.occurrence.name
        if tag in result:
            return None
        result[tag] = read.occurrence.value
    accumulator = owner.profile.accumulator if owner is not None else None
    if accumulator is not None:
        accumulator_writes = tuple(
            write
            for write in projection.writes_for_run(definition.run)
            if write.instruction is definition.instruction
            and write.transition.tag_name == accumulator.name
        )
        if len(accumulator_writes) != 1:
            return None
        result[accumulator.name] = accumulator_writes[0].transition.to_value
    return MappingProxyType(result)


def _post_advance_accumulator_write(
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    owner: AdvanceOwner,
) -> RungWrite | None:
    """The unique exact sibling write of the owner's resulting coordinate."""

    accumulator = owner.profile.accumulator
    if accumulator is None:
        return None
    writes = tuple(
        write
        for write in projection.writes_for_run(definition.run)
        if write.instruction is definition.instruction
        and write.transition.tag_name == accumulator.name
    )
    return writes[0] if len(writes) == 1 else None


def _completion_branch_error(
    owner: AdvanceOwner,
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    observed_operands: Mapping[str, Any],
    boundary: Cmp | AffineCmp,
) -> str | None:
    """Prove that an owned Boolean write came from its completion branch.

    An Advance owner may write the same Done channel while advancing, resetting,
    or executing disabled cleanup. A matching output value alone cannot select
    among those control paths. The owner therefore declares its ordered exact
    entry controls on :class:`AdvanceProfile`; this bridge merely evaluates
    them against the dynamic run and confirms the resulting comparison.
    """

    controls = owner.profile.completion_controls
    if controls is None:
        return "advance owner does not declare exact completion controls"

    # Completion controls use the rung-entry ConditionView. Build that factual
    # value surface from exact direct reads before the selected write, keeping
    # it separate from ``observed_operands`` where Acc is post-advance.
    entry_values: dict[str, Any] = {}
    for read in projection.reads_for_run(definition.run):
        if read.ordinal >= definition.ordinal:
            continue
        tag = read.occurrence.name
        prior = entry_values.get(tag, read.occurrence.value)
        if tag in entry_values and prior != read.occurrence.value:
            return "completion controls have conflicting exact entry reads"
        entry_values[tag] = read.occurrence.value
    for demand in controls:
        actual = (
            True
            if demand.condition is None
            else _eval_expr_from_state(
                _condition_to_expr(demand.condition),
                entry_values,
            )
        )
        expected = bool(demand.value)
        if actual is not expected:
            return (
                "declared completion control has the wrong truth value"
                if actual is not None
                else "declared completion control cannot be proved from exact entry reads"
            )

    accumulator_write = _post_advance_accumulator_write(projection, definition, owner)
    if accumulator_write is None:
        return "advance owner has no unique post-advance accumulator write"
    boundary_value = constraint_holds(boundary, observed_operands)
    written_value = definition.transition.to_value
    if not isinstance(written_value, bool) or boundary_value is None:
        return "advance completion comparison is not exactly Boolean"
    if written_value is not boundary_value:
        return "advance owner Boolean write did not come from its completion comparison"
    return None


def _contains_identity(values: tuple[Any, ...], selected: Any) -> bool:
    return any(value is selected for value in values)


def _instruction_runs(body: tuple[Any, ...]) -> tuple[InstructionRun, ...]:
    result: list[InstructionRun] = []
    for item in body:
        if isinstance(item, InstructionRun):
            result.append(item)
            result.extend(_instruction_runs(item.body))
        elif isinstance(item, LoopIterationRun):
            result.extend(_instruction_runs(item.body))
        elif isinstance(item, RungRun):
            continue
    return tuple(result)


def _unique_static_run(
    projection: ScanRungWriteProjection,
    rung: Any,
) -> RungRun | None:
    runs = tuple(run for run in projection.runs if run.rung is rung)
    return runs[0] if len(runs) == 1 else None


def _exact_guard_cause_run(
    selected: RungRun,
    projection: ScanRungWriteProjection,
) -> RungRun:
    """Climb only exact dynamic parents when a selected branch inherited false."""

    ancestors = tuple(
        sorted(
            (
                candidate
                for candidate in projection.runs
                if candidate is not selected
                and any(nested is selected for nested in candidate.rung_occurrences)
            ),
            key=lambda candidate: candidate.depth,
            reverse=True,
        )
    )
    for candidate in (selected, *ancestors):
        evaluation = _evaluate_run_guard(candidate, projection)
        if (
            not candidate.enabled
            and not evaluation.value
            and evaluation.exact
            and evaluation.requirement is not None
        ):
            return candidate
    return selected


def _guard_explanation(run: RungRun, projection: ScanRungWriteProjection) -> FailureExplanation:
    run = _exact_guard_cause_run(run, projection)
    evaluation = _evaluate_run_guard(run, projection)
    supporting = tuple(occurrence_snapshot(read) for read in evaluation.supporting_reads)
    if run.enabled or evaluation.value:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected dynamic rung was not disabled by its local guard",
            supporting_occurrences=supporting,
        )
    if not evaluation.exact or evaluation.requirement is None:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected false guard frontier is not exactly representable",
            supporting_occurrences=supporting,
        )
    return FailureExplanation(
        FailureExplanationKind.GUARD_FALSE,
        detail="selected dynamic rung guard was false",
        supporting_occurrences=supporting,
    )


def explain_selected_absence(
    observation: Any,
    projection: ScanRungWriteProjection,
    source_checkpoint: Any,
) -> FailureExplanation:
    """Explain only the exact selected absent producer, failing closed.

    The obligation's retained producer rung is never replaced by another
    writer. Spentness is proved from the exact source checkpoint memory key,
    not inferred from the destination or failed landing.
    """

    if getattr(observation, "disposition", None) != "ABSENT":
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="effect observation is not an absent selected writer",
        )
    obligation = getattr(observation, "obligation", None)
    producer_rung = getattr(obligation, "producer_rung", None)
    tag = getattr(obligation, "tag", None)
    if producer_rung is None or tag is None:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected producer identity is incomplete",
        )
    runs = tuple(run for run in projection.runs if run.rung is producer_rung)
    if not runs:
        return FailureExplanation(
            FailureExplanationKind.NOT_EXECUTABLE,
            detail="selected producer has no dynamic occurrence in this execution",
        )
    if len(runs) != 1:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected producer has several dynamic occurrences",
        )
    run = runs[0]
    if not run.enabled:
        return _guard_explanation(run, projection)

    guard_evaluation = _evaluate_run_guard(run, projection)
    supporting = tuple(occurrence_snapshot(read) for read in guard_evaluation.supporting_reads)

    writers = tuple(
        item
        for item in _instruction_runs(run.body)
        if instruction_writes_tag(item.instruction, tag)
    )
    if len(writers) != 1:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected producer write-site identity is ambiguous",
            supporting_occurrences=supporting,
        )
    instruction = writers[0].instruction
    if not bool(getattr(instruction, "oneshot", False)):
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="powered selected writer produced no exact write",
            supporting_occurrences=supporting,
        )
    source_work = getattr(getattr(source_checkpoint, "world", None), "work", None)
    source_state = getattr(source_work, "state", None)
    if source_state is None:
        return FailureExplanation(
            FailureExplanationKind.UNKNOWN,
            detail="selected one-shot source checkpoint is unavailable",
            supporting_occurrences=supporting,
        )
    memory_key = instruction.memory_key("_oneshot")
    if source_state.memory.get(memory_key) is True:
        return FailureExplanation(
            FailureExplanationKind.SPENT,
            detail=f"selected one-shot was spent at source memory {memory_key!r}",
            supporting_occurrences=supporting,
        )
    return FailureExplanation(
        FailureExplanationKind.UNKNOWN,
        detail="selected one-shot was armed but produced no exact write",
        supporting_occurrences=supporting,
    )


def _validate_source_identity(
    *,
    execution_owner: Any,
    source_checkpoint: Any,
    selected_writer: Any,
    projection: ScanRungWriteProjection,
) -> str | None:
    if execution_owner is None:
        return "exact execution epoch owner is required"
    execution_epoch = getattr(execution_owner, "epoch", None)
    if execution_epoch is None:
        return "execution owner does not expose an exact epoch"
    if not (
        getattr(execution_epoch, "first_scan", projection.scan_id + 1)
        <= projection.scan_id
        <= getattr(execution_epoch, "last_scan", projection.scan_id - 1)
    ):
        return "execution epoch does not cover the exact projection"
    if source_checkpoint is None or getattr(source_checkpoint, "owner", None) is None:
        return "source checkpoint identity is required"
    if selected_writer is None:
        return "selected writer identity is required"
    return None


def derive_guard_requirement_from_effect(
    observation: Any,
    projection: ScanRungWriteProjection,
    *,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
) -> RequirementDerivation:
    """Derive the exact false frontier of the selected producer/consumer.

    ``ABSENT`` explains only the retained producer. ``STRANDED`` may explain
    only the retained obliged consumer.  A consumer that ran and read the
    effect names itself directly; an exactly unique disabled consumer run names
    its own false guard even though, by definition, it never reached the effect
    read. Neither arm searches for another producer or consumer.
    """

    source_error = _validate_source_identity(
        execution_owner=execution_owner,
        source_checkpoint=source_checkpoint,
        selected_writer=selected_writer,
        projection=projection,
    )
    if source_error is not None:
        return _unknown(source_error)

    obligation = getattr(observation, "obligation", None)
    if obligation is None or selected_writer != getattr(obligation, "producer", None):
        return _unknown("selected writer does not match the retained obligation")
    disposition = getattr(observation, "disposition", None)
    run: RungRun | None = None
    explanation: FailureExplanation
    guard_scope: tuple[Any, ...]

    if disposition == "ABSENT":
        explanation = explain_selected_absence(observation, projection, source_checkpoint)
        if explanation.kind is not FailureExplanationKind.GUARD_FALSE:
            return RequirementDerivation(explanation)
        run = _unique_static_run(projection, getattr(obligation, "producer_rung", None))
        guard_scope = ("producer_guard", getattr(obligation, "producer", None))
    elif disposition == "STRANDED":
        consumer_read = getattr(observation, "consumer_read", None)
        appeared = getattr(observation, "appeared", None)
        if appeared is None or not any(write is appeared for write in projection.writes):
            return _unknown("stranded effect has no exact selected producer occurrence")
        consumer_rung = getattr(obligation, "consumer_rung", None)
        if consumer_read is not None and any(read is consumer_read for read in projection.reads):
            run = consumer_read.run
            if run.rung is not consumer_rung:
                return _unknown("stranded read does not belong to the obliged consumer")
        else:
            run = _unique_static_run(projection, consumer_rung)
            if run is None or run.enabled:
                return _unknown("stranded effect has no exact disabled consumer run")
        explanation = _guard_explanation(run, projection)
        if explanation.kind is not FailureExplanationKind.GUARD_FALSE:
            return RequirementDerivation(explanation)
        guard_scope = ("consumer_guard", getattr(obligation, "consumer", None))
    else:
        return _unknown("effect is neither absent nor stranded")

    if run is None:
        return _unknown("selected guard has no unique dynamic occurrence")
    run = _exact_guard_cause_run(run, projection)
    evaluation = _evaluate_run_guard(run, projection)
    if not evaluation.exact or evaluation.requirement is None:
        return RequirementDerivation(explanation)
    atoms = _guard_atoms(evaluation.requirement)
    if not atoms or not evaluation.supporting_reads:
        return RequirementDerivation(explanation)

    demanding = occurrence_snapshot(evaluation.supporting_reads[-1])
    condition = _bind_guard_demanding_rung(evaluation.requirement, run.rung)
    source_walk = derive_occurrence_source_requirement(condition, projection)
    return RequirementDerivation(
        explanation=explanation,
        requirement=ActiveRequirement(
            # The transitive source walk is report-only. Production navigation
            # consumes this exact occurrence-local requirement independently.
            condition=condition,
            demanding_occurrence=demanding,
            # A compound false guard is decided at its final observed read.
            # Each OR arm keeps its own (possibly earlier) actionable deadline;
            # Phase 5 must select an arm before applying that atom's deadline.
            deadline=demanding,
            selected_writer=selected_writer,
            operand_authority=OperandAuthority.UNKNOWN,
            execution_owner=execution_owner,
            source_world_key=source_world_key,
            checkpoint_owner=source_checkpoint.owner,
            source_checkpoint=source_checkpoint,
            phase=phase,
            provenance=provenance,
            scope=(*scope, guard_scope),
        ),
        source_walk=source_walk,
    )


def derive_overwriter_guard_requirement_from_effect(
    observation: Any,
    projection: ScanRungWriteProjection,
    *,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
    preserved_values: tuple[tuple[str, Any], ...] = (),
) -> RequirementDerivation:
    """Prevent one exact harmful writer by complementing its observed guard."""

    return derive_overwriter_guard_requirement_from_write(
        getattr(observation, "displacement", None),
        projection,
        disposition=getattr(observation, "disposition", None),
        execution_owner=execution_owner,
        selected_writer=selected_writer,
        source_world_key=source_world_key,
        source_checkpoint=source_checkpoint,
        phase=phase,
        provenance=provenance,
        scope=scope,
        preserved_values=preserved_values,
    )


def derive_overwriter_guard_requirement_from_write(
    displacement: RungWrite | None,
    projection: ScanRungWriteProjection,
    *,
    disposition: str | None = "DISPLACED",
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
    preserved_values: tuple[tuple[str, Any], ...] = (),
) -> RequirementDerivation:
    """Complement the guard of one projection-owned harmful write.

    Effect recovery normally reaches this proof through an observation.  A
    causal checkpoint rebase can already possess the exact harmful write, so
    this narrower seam avoids manufacturing an observation while retaining
    the same source-identity and guard-exactness checks.
    """

    source_error = _validate_source_identity(
        execution_owner=execution_owner,
        source_checkpoint=source_checkpoint,
        selected_writer=selected_writer,
        projection=projection,
    )
    if source_error is not None:
        return _unknown(source_error)
    if disposition not in {"OVERWRITTEN", "DISPLACED"}:
        return _unknown("effect has no harmful overwriter guard to complement")
    if displacement is None or not _contains_identity(projection.writes, displacement):
        return _unknown("harmful overwriter is not owned by the exact projection")
    run = _exact_guard_cause_run(displacement.run, projection)
    if not run.enabled:
        return _unknown("harmful overwriter run was not enabled")
    evaluation = _evaluate_enabling_path_complement(run, projection)
    if (
        not evaluation.value
        or not evaluation.exact
        or evaluation.requirement is None
        or not evaluation.supporting_reads
    ):
        return _unknown("harmful overwriter guard has no exact scalar complement")
    filtered = _residualize_guard_requirement(
        evaluation.requirement,
        preserved_values,
    )
    if filtered is None:
        return _unknown("harmful overwriter guard depends only on excluded channel state")
    condition = _bind_guard_demanding_rung(filtered, run.rung)
    source_walk = derive_occurrence_source_requirement(
        condition,
        projection,
        preserved_values=preserved_values,
    )
    # Only protected terminal tags use the established deadline refinement.
    # The complete transitive walk remains separate report evidence; it is not
    # executable navigation authority.
    condition = _refine_preserved_tag_deadlines(condition, projection, preserved_values)
    demanding = occurrence_snapshot(evaluation.supporting_reads[-1])
    explanation_kind = (
        FailureExplanationKind.OVERWRITTEN
        if disposition == "OVERWRITTEN"
        else FailureExplanationKind.DISPLACED
    )
    refined_support: list[EffectOccurrenceSnapshot] = [
        occurrence_snapshot(read) for read in evaluation.supporting_reads
    ]
    for atom in _guard_atoms(condition):
        for occurrence in (*atom.supporting_occurrences, atom.deadline):
            if occurrence not in refined_support:
                refined_support.append(occurrence)
    explanation = FailureExplanation(
        explanation_kind,
        detail="exact harmful overwriter guard was complemented",
        # Source walking may replace the final overwriter guard with an exact
        # earlier predecessor guard. The failed receipt must carry that atom's
        # proof surface so exact source matching can recover the transaction.
        supporting_occurrences=tuple(refined_support),
    )
    return RequirementDerivation(
        explanation=explanation,
        requirement=ActiveRequirement(
            condition=condition,
            demanding_occurrence=demanding,
            deadline=demanding,
            selected_writer=selected_writer,
            operand_authority=OperandAuthority.UNKNOWN,
            execution_owner=execution_owner,
            source_world_key=source_world_key,
            checkpoint_owner=source_checkpoint.owner,
            source_checkpoint=source_checkpoint,
            phase=phase,
            provenance=provenance,
            scope=(*scope, ("overwriter_guard", occurrence_snapshot(displacement))),
            obstruction_occurrence=occurrence_snapshot(displacement),
        ),
        source_walk=source_walk,
    )


def derive_advance_operand_requirement(
    index: AdvanceIndex,
    channel: str,
    *,
    desired_completion: bool,
    projection: ScanRungWriteProjection,
    definition_write: RungWrite,
    operand_read: RungRead,
    demanding_read: RungRead,
    operand_authority: OperandAuthority,
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    explanation_kind: FailureExplanationKind,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
) -> RequirementDerivation:
    """Derive from one exact dynamic owner definition and causal read.

    Every proof input remains projection-owned until all dynamic identity and
    ordinal checks pass. Only then are occurrences detached into the retained
    requirement.
    """

    source_error = _validate_source_identity(
        execution_owner=execution_owner,
        source_checkpoint=source_checkpoint,
        selected_writer=selected_writer,
        projection=projection,
    )
    if source_error is not None:
        return _unknown(source_error)

    conflicts = index.conflict(channel)
    if conflicts:
        return _unknown(index.conflict_message(channel) or "ambiguous advance owner")
    owner = index.resolve(channel)
    if owner is None:
        return _unknown(f"no advance owner for {channel!r}")

    occurrence_error = _validate_dynamic_occurrences(
        owner,
        channel,
        projection,
        definition_write,
        operand_read,
        demanding_read,
    )
    if occurrence_error is not None:
        return _unknown(occurrence_error)

    observed_operands = _exact_instruction_operands(projection, definition_write, owner)
    if observed_operands is None:
        return _unknown("owner occurrence has repeated operand reads")
    boundary_fn = owner.profile.completion_boundary
    if boundary_fn is None:
        return _unknown("advance owner does not declare a completion boundary")
    boundary = boundary_fn(observed_operands)
    if boundary is None:
        return _unknown("owner completion boundary is unavailable")
    branch_error = _completion_branch_error(
        owner,
        projection,
        definition_write,
        observed_operands,
        boundary,
    )
    if branch_error is not None:
        return _unknown(branch_error)
    relation = boundary if desired_completion else complement_scalar_constraint(boundary)
    if relation is None:
        return _unknown("owner completion boundary cannot be complemented exactly")

    condition = _literal_requirement_for_operand(
        relation,
        operand_read,
        observed_operands,
    )
    if condition is None:
        return _unknown("owner relation cannot be transposed onto the exact operand")

    operand_snapshot = occurrence_snapshot(operand_read)
    demanding_snapshot = occurrence_snapshot(demanding_read)
    accumulator_write = _post_advance_accumulator_write(projection, definition_write, owner)
    assert accumulator_write is not None  # proved by _completion_branch_error
    accumulator_snapshot = occurrence_snapshot(accumulator_write)
    return RequirementDerivation(
        explanation=FailureExplanation(
            explanation_kind,
            supporting_occurrences=(
                operand_snapshot,
                accumulator_snapshot,
                demanding_snapshot,
            ),
        ),
        requirement=ActiveRequirement(
            condition=condition,
            demanding_occurrence=demanding_snapshot,
            deadline=operand_snapshot,
            selected_writer=selected_writer,
            operand_authority=operand_authority,
            execution_owner=execution_owner,
            source_world_key=source_world_key,
            checkpoint_owner=source_checkpoint.owner,
            source_checkpoint=source_checkpoint,
            phase=phase,
            provenance=provenance,
            scope=tuple(scope),
        ),
    )


def derive_advance_requirement_from_effect(
    index: AdvanceIndex,
    projection: ScanRungWriteProjection,
    observation: Any,
    *,
    operand_authorities: Mapping[str, OperandAuthority],
    execution_owner: Any,
    selected_writer: Any,
    source_world_key: Any,
    source_checkpoint: Any,
    phase: RequirementPhase = RequirementPhase.STEADY,
    provenance: str = "",
    scope: tuple[Any, ...] = (),
) -> RequirementDerivation:
    """Follow one exact failed effect to an instruction-owned completion read.

    The effect observation already selected its overwriter/consumer and local
    enabling reads. This adapter considers only consequential Boolean reads
    with one unambiguous Advance owner, then follows their exact same-scan
    definition and chooses an explicitly classified owner operand. It never
    treats arbitrary scan reads as requirements.
    """

    disposition = getattr(observation, "disposition", None)
    if disposition not in {"OVERWRITTEN", "DISPLACED"}:
        return _unknown("effect disposition has no advance completion inversion")
    try:
        explanation_kind = FailureExplanationKind(disposition.lower())
    except ValueError:
        return _unknown("effect disposition has no typed requirement explanation")

    candidates: list[RequirementDerivation] = []
    # These surfaces are complementary.  The immutable displacement closure
    # owns the final writer's exact local/caller guards.  ``observed_reads``
    # may additionally own an exact predecessor chain (for example a nested
    # rollback whose caller consumed an intermediate value produced by an
    # Advance completion).  Replacing the latter with the former loses that
    # conductivity; append only identities not already present instead.
    displacement_reads = tuple(getattr(observation, "displacement_enabling_reads", ()))
    observed_reads = tuple(getattr(observation, "observed_reads", ()))
    demanding_reads = (
        *displacement_reads,
        *(
            read
            for read in observed_reads
            if not any(read is exact for exact in displacement_reads)
        ),
    )
    for demanding_read in demanding_reads:
        channel = demanding_read.occurrence.name
        observed_value = demanding_read.occurrence.value
        if not isinstance(observed_value, bool) or index.resolve(channel) is None:
            continue
        transition = projection.transition_observed_by_read(demanding_read)
        if transition is None or transition.occurrence_ordinal is None:
            continue
        definitions = tuple(
            write
            for write in projection.writes
            if write.ordinal == transition.occurrence_ordinal
            and write.transition.tag_name == channel
            and write.transition.to_value == observed_value
        )
        if len(definitions) != 1:
            continue
        definition = definitions[0]
        owner = index.resolve(channel)
        if owner is None or owner.profile.completion_boundary is None:
            continue
        exact_operands = _exact_instruction_operands(projection, definition, owner)
        if exact_operands is None:
            continue
        boundary = owner.profile.completion_boundary(exact_operands)
        operand_names: tuple[str, ...]
        if isinstance(boundary, Cmp) and boundary.bound_is_tag:
            # Only the distinct bound read is at the same semantic time as the
            # post-advance comparison. The boundary coordinate's sibling read
            # is its entry value and needs a delta inverse Phase 4 does not own.
            operand_names = (str(boundary.bound),)
        else:
            continue
        sibling_reads = projection.reads_observed_by_write(definition)
        for operand_name in operand_names:
            authority = operand_authorities.get(operand_name)
            matching_reads = tuple(
                read for read in sibling_reads if read.occurrence.name == operand_name
            )
            if authority is None or len(matching_reads) != 1:
                continue
            candidates.append(
                derive_advance_operand_requirement(
                    index,
                    channel,
                    desired_completion=not observed_value,
                    projection=projection,
                    definition_write=definition,
                    operand_read=matching_reads[0],
                    demanding_read=demanding_read,
                    operand_authority=authority,
                    execution_owner=execution_owner,
                    selected_writer=selected_writer,
                    source_world_key=source_world_key,
                    source_checkpoint=source_checkpoint,
                    explanation_kind=explanation_kind,
                    phase=phase,
                    provenance=provenance,
                    scope=scope,
                )
            )
            break

    exact = tuple(result for result in candidates if result.requirement is not None)
    if len(exact) == 1:
        return exact[0]
    if len(exact) > 1:
        return _unknown("failed effect has several exact advance inversions")
    if len(candidates) == 1:
        # Preserve the exact owner's fail-closed reason.  The generic fallback
        # obscures which identity/timing check rejected an otherwise unique
        # consequential read and makes causal integration impossible to audit.
        return candidates[0]
    return _unknown("failed effect has no exact advance inversion")


def _validate_dynamic_occurrences(
    owner: AdvanceOwner,
    channel: str,
    projection: ScanRungWriteProjection,
    definition: RungWrite,
    operand: RungRead,
    demand: RungRead,
) -> str | None:
    if not _contains_identity(projection.writes, definition):
        return "owner definition is not owned by the exact projection"
    if not _contains_identity(projection.reads, operand) or not _contains_identity(
        projection.reads, demand
    ):
        return "requirement reads are not owned by the exact projection"
    if not (definition.scan_id == operand.scan_id == demand.scan_id == projection.scan_id):
        return "owner definition and requirement reads cross scan boundaries"
    if definition.transition.tag_name != channel or demand.occurrence.name != channel:
        return "demand does not observe the selected owner channel"
    if (
        definition.instruction is None
        or definition.instruction.instruction is not owner.instruction
    ):
        return "channel definition does not belong to the resolved advance owner"
    sibling_reads = projection.reads_observed_by_write(definition)
    if not _contains_identity(sibling_reads, operand):
        return "selected operand is not an exact owner-instruction read"
    if operand.instruction is not definition.instruction:
        return "selected operand belongs to a different instruction occurrence"
    if operand.run is not definition.run:
        return "dynamic owner operand occurrence identity is inconsistent"
    if not operand.ordinal < definition.ordinal <= demand.ordinal:
        return "owner operand is not timely for the demanding read"

    observed = projection.transition_observed_by_read(demand)
    if observed is None or (
        observed.tag_name != definition.transition.tag_name
        or observed.occurrence_ordinal != definition.ordinal
        or observed.to_value != definition.transition.to_value
    ):
        return "demand did not observe the exact owner definition"
    return None
