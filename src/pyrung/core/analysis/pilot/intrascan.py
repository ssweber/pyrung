"""Interpret one exact scan without choosing or retaining a future.

The execution projection is the semantic oracle.  This module asks what
one already-executed assertion scan proves about an ``EffectExpectation`` and
interprets failed observations with the existing requirement derivations.
Transitive same-scan source walks remain typed evidence; they are never action
choices.  The module does not install a correction, mutate PILOT state, restore
or commit a live world, or retain navigation state.

``derive_recorded_observations`` interprets observations already captured by
the execution owner.  Its filtering, projection selection, and derivation
order intentionally match the former inline implementation in
``pilot.py::_derive_attempt_requirements``.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any

from pyrung.core.analysis.causal._rung_writes import ScanRungWriteProjection
from pyrung.core.analysis.pilot.advance import AdvanceIndex
from pyrung.core.analysis.pilot.effects import (
    EffectObservation,
    EffectObservationSnapshot,
    EffectOccurrenceSelector,
    _instruction_path,
    occurrence_selector,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.execution import CheckpointRef
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.requirement_derivation import (
    bind_guard_condition_operand_authorities,
    bind_guard_operand_authorities,
    derive_advance_requirement_from_effect,
    derive_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveCondition,
    ActiveRequirement,
    ActiveRequirementSnapshot,
    FailureExplanation,
    GuardRequirementAtom,
    GuardRequirementCondition,
    GuardRequirementExpr,
    OperandAuthority,
    RequirementDerivation,
    RequirementSourceWalk,
    RequirementSourceWalkStatus,
)
from pyrung.core.analysis.pilot.world_key import _semantic_key
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.context import RungId
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.intrascan_counterfactual import OccurrenceBoundary
from pyrung.core.runner import EpochRef


@dataclass(frozen=True)
class IntrascanQuestion:
    """Evidence and authority needed to inspect one exact assertion scan."""

    assertion_scan: int
    source_checkpoint: Any = field(compare=False, repr=False)
    advance_index: AdvanceIndex | None = field(compare=False, repr=False)
    operand_authorities: Mapping[str, OperandAuthority] = field(compare=False, repr=False)
    steerable: frozenset[str]
    program_written: frozenset[str]
    configured_inputs: frozenset[str] = frozenset()
    projection_at: Callable[[int], ScanRungWriteProjection | None] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    advance_index_factory: Callable[[], AdvanceIndex] | None = field(
        default=None,
        compare=False,
        repr=False,
    )
    operand_authorities_at: (
        Callable[[ScanRungWriteProjection], Mapping[str, OperandAuthority]] | None
    ) = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class IntrascanFinding:
    """One failed observation and its inert requirement derivation."""

    observation: EffectObservation = field(compare=False, repr=False)
    derivation: RequirementDerivation
    source_checkpoint: Any = field(compare=False, repr=False)

    @property
    def consumed_before_displacement(self) -> bool:
        """Whether the promised value reached one exact read before cleanup.

        This remains report-only evidence.  A caller may use it to distinguish
        an incomplete same-scan handoff from an unconsumed overwrite, but this
        service does not choose a replacement action or retain a continuation.
        """

        observation = self.observation
        requirement = self.derivation.requirement
        if (
            observation.disposition != "OVERWRITTEN"
            or observation.appeared is None
            or observation.displacement is None
            or requirement is None
            or requirement.provenance != "steer-overwriter"
        ):
            return False
        demanding = requirement.demanding_occurrence
        return (
            demanding.kind == "read"
            and demanding.tag == observation.obligation.tag
            and demanding.values == (observation.obligation.value,)
            and demanding.scan_id == observation.appeared.scan_id
            and demanding.scan_id == observation.displacement.scan_id
            and observation.appeared.ordinal < demanding.ordinal < observation.displacement.ordinal
        )

    def diagnostic_snapshot(self) -> IntrascanFindingSnapshot:
        """Detach observation, derivation, source, and causal identity evidence."""

        checkpoint_work = getattr(
            getattr(self.source_checkpoint, "world", None),
            "work",
            None,
        )
        requirement = self.derivation.requirement
        execution_ref = self.observation.execution_ref
        checkpoint_ref = getattr(self.source_checkpoint.owner, "reference", None)
        if not isinstance(execution_ref, EpochRef) or not isinstance(
            checkpoint_ref,
            CheckpointRef,
        ):
            raise ValueError("intrascan finding has no typed execution source")
        return IntrascanFindingSnapshot(
            observation=self.observation.diagnostic_snapshot(),
            explanation=self.derivation.explanation,
            requirement=(requirement.diagnostic_snapshot() if requirement is not None else None),
            selected_writer=self.observation.obligation.producer,
            source_world_key=self.source_checkpoint.key,
            source_scan=getattr(getattr(checkpoint_work, "state", None), "scan_id", None),
            execution_ref=execution_ref,
            checkpoint_ref=checkpoint_ref,
            source_walk=self.derivation.source_walk,
            consumed_before_displacement=self.consumed_before_displacement,
        )


@dataclass(frozen=True)
class IntrascanFindingSnapshot:
    """Detached diagnostic view of one report-only finding."""

    observation: EffectObservationSnapshot
    explanation: FailureExplanation
    requirement: ActiveRequirementSnapshot | None
    selected_writer: Any
    source_world_key: Any
    source_scan: int | None
    execution_ref: EpochRef
    checkpoint_ref: CheckpointRef
    source_walk: RequirementSourceWalk | None
    consumed_before_displacement: bool = False


@dataclass(frozen=True)
class IntrascanResult:
    """Factual observations and derivations for one report-only question."""

    observations: tuple[EffectObservation, ...] = field(compare=False, repr=False)
    findings: tuple[IntrascanFinding, ...]

    def diagnostic_snapshot(self) -> tuple[IntrascanFindingSnapshot, ...]:
        """Return complete detached findings in legacy receipt order."""

        return tuple(finding.diagnostic_snapshot() for finding in self.findings)


class IntrascanRequirementDisposition(StrEnum):
    """Exact demanding-occurrence verdict for one logical requirement."""

    SATISFIED = "satisfied"
    VIOLATED = "violated"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class IntrascanAtomEvidence:
    """One requirement atom and its replay-relocatable read selector."""

    condition: Cmp
    selector: EffectOccurrenceSelector
    deadline: Any


@dataclass(frozen=True)
class IntrascanRequirementEvidence:
    """A complete logical requirement with exact original-read selectors."""

    requirement: ActiveRequirement = field(compare=False, repr=False)
    condition: ActiveCondition
    atoms: tuple[IntrascanAtomEvidence, ...]
    demanding_selector: EffectOccurrenceSelector | None = None
    complete: bool = True
    detail: str = ""

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            self.requirement.identity,
            self.condition,
            tuple((atom.condition, atom.selector, atom.deadline) for atom in self.atoms),
            self.demanding_selector,
            self.complete,
        )


@dataclass(frozen=True)
class IntrascanRequirementObservation:
    """Detached occurrence-relative proof for one candidate scan."""

    requirement_identity: tuple[Any, ...]
    disposition: IntrascanRequirementDisposition
    observed_reads: tuple[Any, ...] = ()
    detail: str = ""


@dataclass(frozen=True)
class IntrascanWriteEvidence:
    """One exact write supplying or resulting from an occurrence requirement."""

    boundary: OccurrenceBoundary
    run_order: int
    ordinal: int
    tag: str
    before: Any
    after: Any
    counterfactual: bool = False

    def matches(self, write: Any) -> bool:
        """Whether an execution projection contains this exact dynamic write."""

        transition = write.transition
        return (
            self.boundary == _occurrence_boundary(write)
            and self.run_order == write.run_order
            and self.ordinal == write.ordinal
            and self.tag == transition.tag_name
            and _values_match(self.before, transition.from_value)
            and _values_match(self.after, transition.to_value)
        )


@dataclass(frozen=True)
class IntrascanReadRequirement:
    """One observed value required by an exact conducting guard ancestry."""

    boundary: OccurrenceBoundary
    run_order: int
    ordinal: int
    tag: str
    value: Any
    source_kind: str
    source_write: IntrascanWriteEvidence | None = None


@dataclass(frozen=True)
class IntrascanProducerTrace:
    """A real preceding program write and the reads which enabled it."""

    write: IntrascanWriteEvidence
    enabling_requirements: tuple[IntrascanReadRequirement, ...]


class IntrascanCausalRelation(StrEnum):
    """How the hypothetical occurrence enabled the useful downstream write."""

    CONDUCTED = "conducted"
    PREVENTED_OVERWRITE = "prevented_overwrite"


@dataclass(frozen=True)
class IntrascanTracebackStep:
    """One evidence-only backward hop from a useful write to its producers."""

    useful_write: IntrascanWriteEvidence
    consumer_requirements: tuple[IntrascanReadRequirement, ...]
    producer_traces: tuple[IntrascanProducerTrace, ...]
    relation: IntrascanCausalRelation = IntrascanCausalRelation.CONDUCTED
    prevented_write: IntrascanWriteEvidence | None = None
    preserved_read: IntrascanReadRequirement | None = None


@dataclass(frozen=True)
class IntrascanProducerGoal:
    """One exact static writer Compass may trace toward from a fresh World.

    The goal is evidence, not a retained route.  ``node_index`` locks a later
    ordinary traceback to the same writer which can supply the hypothetical
    value; Compass must still derive the next act from the current snapshot.
    """

    tag: str
    value: Any
    node_index: int
    rung_id: Any
    branch_path: tuple[int, ...]
    guard_alternatives: tuple[tuple[Any, ...], ...] = ()
    observed_values: tuple[tuple[str, Any], ...] = ()

    @property
    def identity(self) -> tuple[Any, ...]:
        """Detached identity of the exact static producer question."""

        return (
            "intrascan-producer-goal",
            self.tag,
            _semantic_key(self.value),
            self.node_index,
            _semantic_key(self.rung_id),
            self.branch_path,
            _semantic_key(self.guard_alternatives),
            _semantic_key(self.observed_values),
        )


@dataclass(frozen=True)
class IntrascanEdgeRequirement:
    """One exact instruction edge which blocked an otherwise true consumer."""

    boundary: OccurrenceBoundary
    instruction_path: tuple[int, ...]
    memory_key: str
    observed: Any
    required: Any


@dataclass(frozen=True)
class IntrascanBoundaryRealization:
    """Ordinary disposable scan boundary realizing one hypothetical handoff."""

    stage_scan: int | None
    consumer_scan: int | None
    stage_write: IntrascanWriteEvidence | None
    consumer_write: IntrascanWriteEvidence | None
    consumer_assignments: tuple[tuple[str, Any], ...] = ()
    stage_requirements: tuple[IntrascanReadRequirement, ...] = ()
    unresolved_producer_goals: tuple[IntrascanProducerGoal, ...] = ()
    consumer_horizon_read: IntrascanReadRequirement | None = None
    consumer_stop_reached: bool = False
    witnessed: bool = False
    detail: str = ""

    @property
    def direct(self) -> bool:
        return (
            self.witnessed
            and self.stage_scan is None
            and self.stage_write is None
            and self.consumer_write is not None
        )

    @property
    def staged(self) -> bool:
        return self.witnessed and self.stage_scan is not None and self.stage_write is not None


@dataclass(frozen=True)
class _RealizedProducerPatch:
    """Exact value established by an accepted ordinary producer stage."""

    dest: str
    value: Any


@dataclass(frozen=True)
class _RetainedBoundaryRequest:
    """Minimal immutable question needed to reprove a retained consumer."""

    patch: _RealizedProducerPatch
    consumer_assignments: tuple[tuple[str, Any], ...]
    required_condition: Any | None = None


@dataclass(frozen=True)
class IntrascanTracebackWitness:
    """Detached result of one exact occurrence-local hypothetical execution."""

    request_identity: tuple[Any, ...]
    source_scan: int
    assertion_scan: int | None
    applied_exactly_once: bool
    application_values: tuple[tuple[Any, Any], ...] = ()
    downstream_writes: tuple[tuple[Any, ...], ...] = ()
    exit_changes: tuple[tuple[str, Any, Any], ...] = ()
    traceback_step: IntrascanTracebackStep | None = None
    blocked_edges: tuple[IntrascanEdgeRequirement, ...] = ()
    consumer_horizon_read: IntrascanReadRequirement | None = None
    consumer_stop_reached: bool = False
    detail: str = ""


def _occurrence_boundary(access: Any) -> OccurrenceBoundary:
    run = access.run
    return OccurrenceBoundary(
        rung_id=access.rung_id,
        execution_kind=run.kind,
        caller_rung=run.caller_rung,
        call_stack=run.call_stack,
        depth=run.depth,
        call_invocation=access.call_invocation,
        run_order=access.run_order,
        branch_path=getattr(access, "branch_path", None),
    )


def _write_evidence(
    write: Any,
    *,
    counterfactual_write: Any | None,
) -> IntrascanWriteEvidence:
    transition = write.transition
    return IntrascanWriteEvidence(
        boundary=_occurrence_boundary(write),
        run_order=write.run_order,
        ordinal=write.ordinal,
        tag=transition.tag_name,
        before=transition.from_value,
        after=transition.to_value,
        counterfactual=write is counterfactual_write,
    )


def _source_write(projection: Any, read: Any) -> Any | None:
    source = read.occurrence.source
    return next(
        (write for write in projection.writes if write.occurrence is source),
        None,
    )


def _read_requirement(
    projection: Any,
    read: Any,
    *,
    counterfactual_write: Any | None,
) -> IntrascanReadRequirement:
    source_write = _source_write(projection, read)
    return IntrascanReadRequirement(
        boundary=_occurrence_boundary(read),
        run_order=read.run_order,
        ordinal=read.ordinal,
        tag=read.occurrence.name,
        value=read.occurrence.value,
        source_kind=(
            "counterfactual_write"
            if source_write is not None and source_write is counterfactual_write
            else "program_write"
            if source_write is not None
            else str(read.occurrence.source)
        ),
        source_write=(
            _write_evidence(
                source_write,
                counterfactual_write=counterfactual_write,
            )
            if source_write is not None
            else None
        ),
    )


def _counterfactual_patch_write(
    projection: Any,
    request: Any,
    application: Any,
) -> Any | None:
    """Return the one synthetic write which realizes this exact hypothesis."""

    matches = tuple(
        write
        for write in projection.writes
        if write.run_order == application.run_order
        and write.instruction is None
        and write.transition.tag_name == request.patch.dest
        and _values_match(write.transition.from_value, application.before)
        and _values_match(write.transition.to_value, application.after)
    )
    return matches[0] if len(matches) == 1 else None


def _snapshot_boundary(occurrence: Any) -> OccurrenceBoundary:
    """Translate one detached occurrence receipt to its dynamic boundary."""

    subroutine, rung_index = occurrence.rung
    return OccurrenceBoundary(
        rung_id=RungId(subroutine, rung_index),
        execution_kind=occurrence.execution_kind,
        caller_rung=occurrence.caller_rung,
        call_stack=tuple(occurrence.call_stack),
        depth=occurrence.depth,
        call_invocation=occurrence.call_invocation,
        run_order=occurrence.run_order,
        branch_path=(tuple(occurrence.branch_path) if occurrence.branch_path is not None else None),
    )


def _traceback_step(
    projection: Any,
    request: Any,
    application: Any,
) -> IntrascanTracebackStep | None:
    """Derive one exact useful-write → guard → producer hop."""

    counterfactual_write = _counterfactual_patch_write(
        projection,
        request,
        application,
    )
    if counterfactual_write is None:
        return None
    conducted = tuple(
        write
        for write in projection.writes
        if write.ordinal > counterfactual_write.ordinal
        and not _values_match(write.transition.from_value, write.transition.to_value)
        and any(
            _source_write(projection, read) is counterfactual_write
            for read in projection.enabling_read_closure_observed_by_write(write)
        )
    )
    if not conducted:
        return None
    # One instruction may first update its private edge memory and then write
    # the user-visible destination.  The last changed write in the first exact
    # conducted instruction is its physical effect boundary.  Do not constrain
    # that consumer to the patched rung's subtree: a PLC tag conducts across
    # later sibling rungs in occurrence order.
    first = conducted[0]
    instruction_group = tuple(
        write
        for write in conducted
        if write.run_order == first.run_order and write.instruction is first.instruction
    )
    useful = instruction_group[-1]
    consumer_reads = projection.enabling_read_closure_observed_by_write(useful)
    consumer_requirements = tuple(
        _read_requirement(
            projection,
            read,
            counterfactual_write=counterfactual_write,
        )
        for read in consumer_reads
    )
    producer_writes: list[Any] = []
    for requirement in consumer_requirements:
        source = requirement.source_write
        if source is None or source.counterfactual:
            continue
        writer = projection.write_at_ordinal(source.ordinal)
        if writer is not None and all(writer is not prior for prior in producer_writes):
            producer_writes.append(writer)
    producer_traces = tuple(
        IntrascanProducerTrace(
            write=_write_evidence(
                writer,
                counterfactual_write=counterfactual_write,
            ),
            enabling_requirements=tuple(
                _read_requirement(
                    projection,
                    read,
                    counterfactual_write=counterfactual_write,
                )
                for read in projection.enabling_read_closure_observed_by_write(writer)
            ),
        )
        for writer in producer_writes
    )
    return IntrascanTracebackStep(
        useful_write=_write_evidence(
            useful,
            counterfactual_write=counterfactual_write,
        ),
        consumer_requirements=consumer_requirements,
        producer_traces=producer_traces,
    )


def _prevention_traceback_step(
    projection: Any,
    request: Any,
    application: Any,
) -> IntrascanTracebackStep | None:
    """Prove that suppressing one exact overwrite preserved a later input.

    This is deliberately stricter than noticing that a write disappeared. The
    exact patched guard must read the synthetic value and become false, the
    receipt-named harmful transition must disappear at that same boundary, and
    a later changed write must read the harmful write's *before* value from an
    earlier real source, or that exact earlier source must remain at scan exit.
    Together those receipts are the negative equivalent of a direct
    conductivity edge.
    """

    prevented = getattr(request, "prevented_write", None)
    if (
        prevented is None
        or prevented.kind != "write"
        or len(prevented.values) != 2
        or not request.patch.boundary.relocates(_snapshot_boundary(prevented))
    ):
        return None
    counterfactual_write = _counterfactual_patch_write(
        projection,
        request,
        application,
    )
    if counterfactual_write is None:
        return None
    patched_run = projection.runs[application.run_order]
    if patched_run.enabled:
        return None
    patch_reads = tuple(
        read
        for read in projection.reads_for_run(patched_run)
        if _source_write(projection, read) is counterfactual_write
    )
    if not patch_reads:
        return None
    before, after = prevented.values
    if any(
        request.patch.boundary.relocates(_occurrence_boundary(write))
        and write.transition.tag_name == prevented.tag
        and _values_match(write.transition.from_value, before)
        and _values_match(write.transition.to_value, after)
        for write in projection.writes
    ):
        return None

    useful: Any | None = None
    preserved: Any | None = None
    for write in projection.writes:
        if write.ordinal <= counterfactual_write.ordinal or _values_match(
            write.transition.from_value, write.transition.to_value
        ):
            continue
        for read in projection.enabling_read_closure_observed_by_write(write):
            if read.occurrence.domain != "tag" or read.occurrence.name != prevented.tag:
                continue
            if not _values_match(read.occurrence.value, before):
                continue
            source_write = _source_write(projection, read)
            if source_write is not None and source_write.ordinal >= counterfactual_write.ordinal:
                continue
            useful = write
            preserved = read
            break
        if useful is not None:
            break
    if useful is None or preserved is None:
        # The prevented overwrite may itself be the final hazard: no later
        # consumer needs to rewrite the preserved value because the earlier
        # real producer now survives to the scan boundary.  Bind that result
        # to the exact disabled guard read and its real source write.
        retained_reads_list: list[Any] = []
        for read in projection.reads_for_run(patched_run):
            if (
                read.occurrence.domain != "tag"
                or read.occurrence.name != prevented.tag
                or not _values_match(read.occurrence.value, before)
            ):
                continue
            source_write = _source_write(projection, read)
            if source_write is not None and source_write.ordinal < counterfactual_write.ordinal:
                retained_reads_list.append(read)
        retained_reads = tuple(retained_reads_list)
        retained_source = (
            _source_write(projection, retained_reads[0]) if len(retained_reads) == 1 else None
        )
        if (
            retained_source is not None
            and _values_match(retained_source.transition.to_value, before)
            and _values_match(projection.exit_tags.get(prevented.tag), before)
        ):
            useful = retained_source
            preserved = retained_reads[0]
    if useful is None or preserved is None:
        return None

    # As with positive conductivity, retain the physical effect boundary when
    # one instruction performs multiple writes.
    instruction_group = tuple(
        write
        for write in projection.writes
        if write.run_order == useful.run_order
        and write.instruction is useful.instruction
        and write.ordinal >= useful.ordinal
        and not _values_match(write.transition.from_value, write.transition.to_value)
    )
    useful = instruction_group[-1] if instruction_group else useful
    consumer_requirements = tuple(
        _read_requirement(
            projection,
            read,
            counterfactual_write=counterfactual_write,
        )
        for read in projection.enabling_read_closure_observed_by_write(useful)
    )
    preserved_requirement = _read_requirement(
        projection,
        preserved,
        counterfactual_write=counterfactual_write,
    )
    return IntrascanTracebackStep(
        useful_write=_write_evidence(
            useful,
            counterfactual_write=counterfactual_write,
        ),
        consumer_requirements=consumer_requirements,
        # The open producer question is the condition which suppressed the
        # overwrite, not the already-observed source of the preserved value.
        producer_traces=(),
        relation=IntrascanCausalRelation.PREVENTED_OVERWRITE,
        prevented_write=IntrascanWriteEvidence(
            boundary=_snapshot_boundary(prevented),
            run_order=prevented.run_order,
            ordinal=prevented.ordinal,
            tag=prevented.tag,
            before=before,
            after=after,
        ),
        preserved_read=preserved_requirement,
    )


def _blocked_consumer_edges(
    projection: Any,
    request: Any,
    application: Any,
) -> tuple[IntrascanEdgeRequirement, ...]:
    """Detach exact true one-shot memories which suppressed the consumer."""

    blocked: list[IntrascanEdgeRequirement] = []
    for read in projection.reads_for_run(projection.runs[application.run_order]):
        instruction_run = read.instruction
        instruction = instruction_run.instruction if instruction_run is not None else None
        if (
            read.occurrence.domain != "memory"
            or instruction_run is None
            or instruction is None
            or not getattr(instruction, "_oneshot", False)
            or read.occurrence.name != instruction.memory_key("_oneshot")
            or not _values_match(read.occurrence.value, True)
            or not request.patch.boundary.relocates(_occurrence_boundary(read))
        ):
            continue
        instruction_path = _instruction_path(read.run, instruction_run)
        if instruction_path is None:
            continue
        changed = any(
            write.instruction is instruction_run
            and write.ordinal > read.ordinal
            and not _values_match(write.transition.from_value, write.transition.to_value)
            for write in projection.writes_for_run(read.run)
        )
        if changed:
            continue
        requirement = IntrascanEdgeRequirement(
            boundary=_occurrence_boundary(read),
            instruction_path=instruction_path,
            memory_key=read.occurrence.name,
            observed=read.occurrence.value,
            required=False,
        )
        if requirement not in blocked:
            blocked.append(requirement)
    return tuple(blocked)


def _traceback_miss_detail(
    projection: Any,
    request: Any,
    application: Any,
) -> str:
    """Explain a bounded exact-write/source-link miss for watcher diagnosis."""

    patch_writes = tuple(
        write
        for write in projection.writes_for_run(projection.runs[application.run_order])
        if write.instruction is None
        and write.transition.tag_name == request.patch.dest
        and _values_match(write.transition.from_value, application.before)
        and _values_match(write.transition.to_value, application.after)
    )
    changed = tuple(
        write
        for write in projection.writes
        if write.ordinal > (patch_writes[0].ordinal if len(patch_writes) == 1 else -1)
        and not _values_match(write.transition.from_value, write.transition.to_value)
    )[:4]
    candidates = tuple(
        (
            write.run_order,
            write.transition.tag_name,
            write.transition.from_value,
            write.transition.to_value,
            tuple(
                (
                    read.run_order,
                    read.occurrence.name,
                    read.occurrence.value,
                    (
                        ("write", read.occurrence.source.ordinal)
                        if hasattr(read.occurrence.source, "ordinal")
                        else read.occurrence.source
                    ),
                )
                for read in projection.enabling_read_closure_observed_by_write(write)
            ),
        )
        for write in changed
    )
    return f"patch_writes={len(patch_writes)} changed={candidates!r}"


def _matching_write(projection: Any, expected: IntrascanWriteEvidence) -> Any | None:
    matches = tuple(
        write
        for write in projection.writes
        if _occurrence_boundary(write) == expected.boundary
        and write.transition.tag_name == expected.tag
        and write.transition.from_value == expected.before
        and write.transition.to_value == expected.after
    )
    return matches[0] if len(matches) == 1 else None


def _request_value_satisfied(request: Any, value: Any) -> bool:
    """Whether one real value realizes the hypothetical's physical demand."""

    condition = getattr(request, "required_condition", None)
    if condition is None:
        return _values_match(value, request.patch.value)
    if not isinstance(condition, Cmp) or condition.tag != request.patch.dest:
        return False
    return constraint_holds(condition, {request.patch.dest: value}) is True


def _natural_consumer_horizon_read(
    projection: Any,
    request: Any,
) -> Any | None:
    """Return the exact consumer read when ordinary execution already satisfies it.

    A scan-start steer can enable a real program producer before the historical
    consumer is evaluated.  In that case the analysis-only patch correctly
    remains inactive.  Preserve the real read/source receipt instead of
    manufacturing a no-op counterfactual write and stealing its conductivity.
    """

    matches = tuple(
        read
        for read in projection.reads
        if read.occurrence.name == request.patch.dest
        and request.patch.boundary.relocates(_occurrence_boundary(read))
        and _request_value_satisfied(request, read.occurrence.value)
    )
    return matches[0] if len(matches) == 1 else None


def _entry_value(source: Any, name: str) -> Any:
    """Read one scan-entry value from its real state store.

    Trace projections name tag reads and hidden instruction-memory reads in
    the same requirement stream.  PLC snapshots keep those in separate maps;
    an absent one-shot key has the executor's ordinary rearmed value, false.
    """

    state = source.state
    if name in state.tags:
        return state.tags[name]
    if name in state.memory:
        return state.memory[name]
    if name.startswith("_oneshot:"):
        return False
    return None


def research_intrascan_boundary_realization(
    request: Any,
    witness: IntrascanTracebackWitness,
    source: Any,
    pilot_rungs: Sequence[PilotRung],
) -> IntrascanBoundaryRealization:
    """Test a direct consumer or producer→consumer realization without writes."""

    if (
        witness.consumer_stop_reached
        and witness.consumer_horizon_read is not None
        and witness.assertion_scan == witness.source_scan + 1
    ):
        assignments = tuple(request.consumer_assignments) or (
            (request.patch.dest, request.patch.value),
        )
        return IntrascanBoundaryRealization(
            stage_scan=None,
            consumer_scan=witness.assertion_scan,
            stage_write=None,
            consumer_write=None,
            consumer_assignments=assignments,
            consumer_horizon_read=witness.consumer_horizon_read,
            consumer_stop_reached=True,
            witnessed=True,
            detail=(
                "ordinary execution established the required value before the "
                "exact consumer boundary"
            ),
        )

    step = witness.traceback_step
    if step is None or len(step.producer_traces) > 1:
        return IntrascanBoundaryRealization(
            None,
            None,
            None,
            None,
            detail="boundary realization requires one unambiguous producer path",
        )
    assignments = tuple(request.consumer_assignments) or (
        (request.patch.dest, request.patch.value),
    )
    if not step.producer_traces:
        # A program-owned hypothetical may have a real producer in an ordinary
        # prior scan even though that writer is disabled by the consumer act in
        # the hypothetical scan. Observe that stage independently and require
        # one exact retained write before treating it as a realization.
        prevention_ready = (
            step.relation is IntrascanCausalRelation.PREVENTED_OVERWRITE
            and _request_value_satisfied(
                request,
                source.state.tags.get(request.patch.dest),
            )
        )
        if not prevention_ready:
            producer = _natural_stage_producer(
                request,
                source,
                pilot_rungs,
            )
            if producer is not None:
                return _realize_staged_boundary(
                    source,
                    pilot_rungs,
                    producer,
                    step.useful_write,
                    assignments,
                )

            producer_goals = _unresolved_producer_goals(request, source, pilot_rungs)
            if producer_goals:
                return IntrascanBoundaryRealization(
                    None,
                    None,
                    None,
                    None,
                    consumer_assignments=assignments,
                    unresolved_producer_goals=producer_goals,
                    detail=(
                        "the hypothetical condition has exact program writers, but "
                        "its producer preconditions require ordinary navigation"
                    ),
                )

            entry_requirements = tuple(
                requirement
                for requirement in step.consumer_requirements
                if requirement.source_kind != "counterfactual_write"
            )
            if any(requirement.source_kind != "entry" for requirement in entry_requirements):
                return IntrascanBoundaryRealization(
                    None,
                    None,
                    None,
                    None,
                    detail="direct consumer has an unresolved non-entry requirement",
                )
            if any(
                not _values_match(_entry_value(source, requirement.tag), requirement.value)
                for requirement in entry_requirements
            ):
                return IntrascanBoundaryRealization(
                    None,
                    None,
                    None,
                    None,
                    detail=("source World does not satisfy the direct consumer requirements"),
                )
        from pyrung.core.executor import ConditionViewCapture

        consumer_scan: int | None = None
        try:
            fork = fork_with_pilot_rungs(source, pilot_rungs, history_budget=math.inf)
            fork.patch(dict(assignments))
            consumer_capture = ConditionViewCapture()
            fork._run_single_scan(
                consume_pause_request=False,
                execution_capture=consumer_capture,
            )
            consumer_scan = fork.state.scan_id
            projection = fork._projection_from_capture(consumer_scan, consumer_capture)
            if projection is None:
                raise ValueError("direct consumer projection is unavailable")
            consumer_write = _matching_write(projection, step.useful_write)
            if consumer_write is None:
                raise ValueError("direct consumer write was not reproduced exactly")
            if not _values_match(
                fork.state.tags.get(step.useful_write.tag),
                step.useful_write.after,
            ):
                raise ValueError("direct consumer value did not survive the scan boundary")
        except Exception as exc:  # noqa: BLE001 - bounded disposable proof fails closed
            return IntrascanBoundaryRealization(
                None,
                consumer_scan,
                None,
                None,
                consumer_assignments=assignments,
                detail=f"direct boundary realization was unavailable: {type(exc).__name__}",
            )
        return IntrascanBoundaryRealization(
            None,
            consumer_scan,
            None,
            _write_evidence(consumer_write, counterfactual_write=None),
            consumer_assignments=assignments,
            witnessed=True,
            detail="one ordinary scan-start steer reproduced the exact consumer write",
        )

    return _realize_staged_boundary(
        source,
        pilot_rungs,
        step.producer_traces[0],
        step.useful_write,
        assignments,
    )


def research_retained_frontier_realization(
    request: Any,
    source: Any,
    pilot_rungs: Sequence[PilotRung],
) -> IntrascanBoundaryRealization:
    """Reprove a frontier consumer after its exact producer stage advanced.

    The accepted ordinary attempt—not the earlier disposable value—is now the
    physical scan-start fact.  Reuse only the retained backward witness and the
    exact selected goal; no counterfactual is executed in this new World.
    """

    frontier = request.traceback_frontier
    goal = request.producer_goal
    owned = tuple(item for item in frontier.producer_goals if item.identity == goal.identity)
    if len(owned) != 1 or not _values_match(source.state.tags.get(goal.tag), goal.value):
        return IntrascanBoundaryRealization(
            None,
            None,
            None,
            None,
            detail="accepted producer goal is not present in the current World",
        )
    boundary_request = _RetainedBoundaryRequest(
        patch=_RealizedProducerPatch(goal.tag, goal.value),
        consumer_assignments=frontier.consumer_assignments,
    )
    return research_intrascan_boundary_realization(
        boundary_request,
        frontier.witness,
        source,
        pilot_rungs,
    )


def _unresolved_producer_goals(
    request: Any,
    source: Any,
    pilot_rungs: Sequence[PilotRung],
    *,
    limit: int = 8,
) -> tuple[IntrascanProducerGoal, ...]:
    """Detach bounded exact writers for a value unavailable in this World."""

    from pyrung.core.analysis.pdg import resolve_rung
    from pyrung.core.analysis.prove.expr import _eval_expr_from_state
    from pyrung.core.analysis.simplified import (
        And,
        Atom,
        Const,
        Or,
        _sp_to_expr,
    )
    from pyrung.core.analysis.sp_values import writer_value_facts
    from pyrung.core.context import RungId
    from pyrung.core.executor import ConditionViewCapture

    program = getattr(source, "program", None)
    ensure_pdg = getattr(source, "_ensure_pdg", None)
    if program is None or ensure_pdg is None or limit <= 0:
        return ()
    try:
        pdg = ensure_pdg()
        facts = tuple(
            fact
            for fact in writer_value_facts(program, pdg).get(request.patch.dest, ())
            if _request_value_satisfied(request, fact.written_value)
        )
        fork = fork_with_pilot_rungs(source, pilot_rungs, history_budget=math.inf)
        capture = ConditionViewCapture()
        fork._run_single_scan(
            consume_pause_request=False,
            execution_capture=capture,
        )
        projection = fork._projection_from_capture(fork.state.scan_id, capture)
    except Exception:  # noqa: BLE001 - detached research fails closed
        return ()
    if not facts or len(facts) > limit or projection is None:
        return ()

    def alternatives(expr: Any) -> tuple[tuple[Any, ...], ...] | None:
        if isinstance(expr, Const):
            return ((),) if expr.value else ()
        if isinstance(expr, Atom):
            if expr.unsupported is not None:
                return None
            return ((expr,),)
        if isinstance(expr, Or):
            result: list[tuple[Any, ...]] = []
            for term in expr.terms:
                branch = alternatives(term)
                if branch is None:
                    return None
                result.extend(branch)
                if len(result) > limit:
                    return None
            return tuple(result)
        if not isinstance(expr, And):
            return None
        result = [()]
        for term in expr.terms:
            branches = alternatives(term)
            if branches is None:
                return None
            result = [(*prefix, *branch) for prefix in result for branch in branches]
            if len(result) > limit:
                return None
        return tuple(result)

    goals: list[IntrascanProducerGoal] = []
    for fact in facts:
        if fact.node_index < 0 or fact.node_index >= len(pdg.rung_nodes):
            return ()
        node = pdg.rung_nodes[fact.node_index]
        rung = resolve_rung(program, node)
        if rung is None:
            continue
        sp = rung.sp_tree()
        guard_alternatives = alternatives(Const(True) if sp is None else _sp_to_expr(sp))
        if not guard_alternatives:
            continue
        run_orders = tuple(
            dict.fromkeys(
                read.run_order
                for read in projection.reads
                if read.rung_id == RungId(node.subroutine, node.rung_index)
                and tuple(read.branch_path) == tuple(node.branch_path)
                and read.run.call_stack == request.patch.boundary.call_stack
                and read.call_invocation == request.patch.boundary.call_invocation
            )
        )
        if len(run_orders) != 1:
            continue
        observed = {
            read.occurrence.name: read.occurrence.value
            for read in projection.reads_for_run(projection.runs[run_orders[0]])
            if read.occurrence.domain == "tag"
        }
        exact_alternatives = tuple(
            alternative
            for alternative in guard_alternatives
            if alternative
            and all(
                atom.tag in observed
                and (
                    not atom.operand_is_tag
                    or isinstance(atom.operand, str)
                    and atom.operand in observed
                )
                for atom in alternative
            )
            and any(_eval_expr_from_state(atom, observed) is False for atom in alternative)
        )
        if not exact_alternatives:
            continue
        goal = IntrascanProducerGoal(
            tag=fact.tag,
            value=fact.written_value,
            node_index=fact.node_index,
            rung_id=RungId(node.subroutine, node.rung_index),
            branch_path=tuple(node.branch_path),
            guard_alternatives=exact_alternatives,
            observed_values=tuple(observed.items()),
        )
        if goal not in goals:
            goals.append(goal)
    return tuple(goals)


def _natural_stage_producer(
    request: Any,
    source: Any,
    pilot_rungs: Sequence[PilotRung],
) -> IntrascanProducerTrace | None:
    """Detach one real scan-boundary writer for an internal hypothetical."""

    from pyrung.core.executor import ConditionViewCapture

    try:
        fork = fork_with_pilot_rungs(source, pilot_rungs, history_budget=math.inf)
        capture = ConditionViewCapture()
        fork._run_single_scan(
            consume_pause_request=False,
            execution_capture=capture,
        )
        scan = fork.state.scan_id
        projection = fork._projection_from_capture(scan, capture)
        if projection is None or not _request_value_satisfied(
            request,
            fork.state.tags.get(request.patch.dest),
        ):
            return None
        matches = tuple(
            write
            for write in projection.writes
            if write.transition.tag_name == request.patch.dest
            and not _values_match(write.transition.from_value, write.transition.to_value)
            and _request_value_satisfied(request, write.transition.to_value)
        )
        if len(matches) != 1:
            return None
        writer = matches[0]
        return IntrascanProducerTrace(
            write=_write_evidence(writer, counterfactual_write=None),
            enabling_requirements=tuple(
                _read_requirement(
                    projection,
                    read,
                    counterfactual_write=None,
                )
                for read in projection.enabling_read_closure_observed_by_write(writer)
            ),
        )
    except Exception:  # noqa: BLE001 - bounded disposable proof fails closed
        return None


def _realize_staged_boundary(
    source: Any,
    pilot_rungs: Sequence[PilotRung],
    producer: IntrascanProducerTrace,
    useful_write: IntrascanWriteEvidence,
    assignments: tuple[tuple[str, Any], ...],
) -> IntrascanBoundaryRealization:
    """Prove one ordinary producer scan followed by one consumer steer."""

    entry_requirements = tuple(
        requirement
        for requirement in producer.enabling_requirements
        if requirement.source_kind == "entry"
    )
    if any(
        not _values_match(_entry_value(source, requirement.tag), requirement.value)
        for requirement in entry_requirements
    ):
        return IntrascanBoundaryRealization(
            None,
            None,
            None,
            None,
            detail="source World does not satisfy the producer's observed entry requirements",
        )

    from pyrung.core.executor import ConditionViewCapture

    stage_scan: int | None = None
    consumer_scan: int | None = None
    try:
        fork = fork_with_pilot_rungs(source, pilot_rungs, history_budget=math.inf)
        stage_capture = ConditionViewCapture()
        fork._run_single_scan(
            consume_pause_request=False,
            execution_capture=stage_capture,
        )
        stage_scan = fork.state.scan_id
        stage_projection = fork._projection_from_capture(stage_scan, stage_capture)
        if stage_projection is None:
            raise ValueError("stage projection is unavailable")
        stage_write = _matching_write(stage_projection, producer.write)
        if stage_write is None:
            raise ValueError("stage producer write was not reproduced exactly")
        if not _values_match(fork.state.tags.get(producer.write.tag), producer.write.after):
            raise ValueError("stage producer value did not survive the scan boundary")

        fork.patch(dict(assignments))
        consumer_capture = ConditionViewCapture()
        fork._run_single_scan(
            consume_pause_request=False,
            execution_capture=consumer_capture,
        )
        consumer_scan = fork.state.scan_id
        consumer_projection = fork._projection_from_capture(
            consumer_scan,
            consumer_capture,
        )
        if consumer_projection is None:
            raise ValueError("consumer projection is unavailable")
        consumer_write = _matching_write(consumer_projection, useful_write)
        if consumer_write is None:
            raise ValueError("consumer write was not reproduced exactly")
    except Exception as exc:  # noqa: BLE001 - bounded disposable proof fails closed
        return IntrascanBoundaryRealization(
            stage_scan,
            consumer_scan,
            None,
            None,
            consumer_assignments=assignments,
            detail=f"boundary realization was unavailable: {type(exc).__name__}: {exc}",
        )

    return IntrascanBoundaryRealization(
        stage_scan=stage_scan,
        consumer_scan=consumer_scan,
        stage_write=_write_evidence(stage_write, counterfactual_write=None),
        consumer_write=_write_evidence(consumer_write, counterfactual_write=None),
        consumer_assignments=assignments,
        stage_requirements=producer.enabling_requirements,
        witnessed=True,
        detail="two ordinary scans reproduced the staged producer/consumer handoff",
    )


def research_intrascan_traceback(
    request: Any,
    source: Any,
    pilot_rungs: Sequence[PilotRung],
) -> IntrascanTracebackWitness:
    """Execute one hypothetical on a disposable fork and detach its evidence."""

    from pyrung.core.executor import ConditionViewCapture
    from pyrung.core.intrascan_counterfactual import execute_counterfactual_program

    source_scan = source.state.scan_id
    try:
        fork = fork_with_pilot_rungs(
            source,
            pilot_rungs,
            history_budget=math.inf,
        )
        consumer_assignments = tuple(request.consumer_assignments) or (
            (request.patch.dest, request.patch.value),
        )
        fork.patch(
            dict((tag, value) for tag, value in consumer_assignments if tag != request.patch.dest)
        )
        capture = ConditionViewCapture()
        ctx, dt = fork._prepare_scan(synthesis_observer=capture)
        if fork.program is None:
            raise ValueError("counterfactual traceback requires a Program")
        receipt = execute_counterfactual_program(
            fork.program,
            ctx,
            (request.patch,),
            capture=capture,
        )
        result = ctx.commit(dt=dt)
        capture.exit_tags = result.tags
        projection = fork._projection_from_capture(
            result.scan_id,
            capture,
            include_memory_reads=True,
        )
    except Exception as exc:  # noqa: BLE001 - analysis-only execution fails closed
        return IntrascanTracebackWitness(
            request_identity=request.identity,
            source_scan=source_scan,
            assertion_scan=None,
            applied_exactly_once=False,
            detail=f"counterfactual execution was unavailable: {type(exc).__name__}",
        )

    applications = receipt.applications_for(request.patch)
    exact = len(applications) == 1 and projection is not None
    natural_consumer_read = (
        _natural_consumer_horizon_read(projection, request)
        if not applications and projection is not None
        else None
    )
    stop_reached = natural_consumer_read is not None
    consumer_horizon_read = (
        _read_requirement(
            projection,
            natural_consumer_read,
            counterfactual_write=None,
        )
        if natural_consumer_read is not None and projection is not None
        else None
    )
    traceback_step = None
    if exact:
        traceback_step = _traceback_step(projection, request, applications[0])
        if traceback_step is None:
            traceback_step = _prevention_traceback_step(
                projection,
                request,
                applications[0],
            )
    blocked_edges = (
        _blocked_consumer_edges(projection, request, applications[0])
        if exact and traceback_step is None
        else ()
    )
    traceback_miss = (
        _traceback_miss_detail(projection, request, applications[0])
        if exact and traceback_step is None and not blocked_edges
        else ""
    )
    downstream: list[tuple[Any, ...]] = []
    if exact:
        start = applications[0].run_order
        for run_order, run in enumerate(capture.runs):
            if run_order < start:
                continue
            for write in run.direct_write_occurrences:
                if (
                    run_order == start
                    and write.name == request.patch.dest
                    and write.before == applications[0].before
                    and write.after == applications[0].after
                ):
                    continue
                if len(downstream) >= 64:
                    continue
                downstream.append(
                    (
                        run_order,
                        (run.rung_id.subroutine, run.rung_id.rung_index),
                        run.kind,
                        run.caller_rung,
                        write.name,
                        write.before,
                        write.after,
                    )
                )
    entry = dict(capture.entry_tags or {})
    exit_tags = dict(result.tags)
    exit_changes = tuple(
        (name, entry.get(name), value)
        for name, value in sorted(exit_tags.items())
        if entry.get(name) != value
    )
    encountered = tuple(
        (
            boundary.caller_rung,
            boundary.call_stack,
            boundary.depth,
            boundary.call_invocation,
            boundary.run_order,
            boundary.branch_path,
        )
        for patch, boundary in receipt.encountered_candidates
        if patch == request.patch
    )
    return IntrascanTracebackWitness(
        request_identity=request.identity,
        source_scan=source_scan,
        assertion_scan=result.scan_id,
        applied_exactly_once=exact,
        application_values=tuple((item.before, item.after) for item in applications),
        downstream_writes=tuple(downstream),
        exit_changes=exit_changes,
        traceback_step=traceback_step,
        blocked_edges=blocked_edges,
        consumer_horizon_read=consumer_horizon_read,
        consumer_stop_reached=stop_reached,
        detail=(
            "exact occurrence-local hypothetical and one backward hop were observed"
            if traceback_step is not None
            else "exact consumer was blocked by retained instruction edge state"
            if blocked_edges
            else "ordinary execution reached the exact consumer with its required value"
            if stop_reached
            else "exact occurrence-local hypothetical had no useful downstream handoff"
            if exact
            else (
                f"hypothetical matched {len(applications)} dynamic occurrences; "
                f"same-rung boundaries={encountered!r}"
            )
        )
        + (f"; {traceback_miss}" if traceback_miss else ""),
    )


def derive_recorded_observations(
    question: IntrascanQuestion,
    observations: Sequence[EffectObservation],
    *,
    fallback_scan: int | None = None,
) -> IntrascanResult:
    """Interpret already-recorded observations without making a decision.

    A surviving occurrence suppresses another failure for the same obligation
    object, just as the legacy adapter did.  Each remaining observation must
    carry an exact execution owner and epoch, and its selected projection must
    match the latest involved occurrence scan.  Replaying that exact scan is
    the sole fallback; unavailable evidence is ignored rather than guessed.
    """

    recorded = tuple(observations)
    fulfilled_obligations = {
        id(item.obligation) for item in recorded if item.disposition == "SURVIVED"
    }
    findings: list[IntrascanFinding] = []
    advance_index = question.advance_index
    for observation in recorded:
        if (
            observation.disposition in {"SURVIVED", "SUBSUMED"}
            or id(observation.obligation) in fulfilled_obligations
        ):
            continue
        owner = observation.execution_owner
        epoch = observation.execution_epoch
        if owner is None or epoch is None:
            continue
        scans = tuple(
            occurrence.scan_id
            for occurrence in (
                observation.appeared,
                observation.consumer_read,
                observation.displacement,
                observation.displaced_read,
                *observation.observed_reads,
            )
            if occurrence is not None
        )
        scan = max(
            scans, default=question.assertion_scan if fallback_scan is None else fallback_scan
        )
        projection = observation.execution_projection
        if projection is None or projection.scan_id != scan:
            project = question.projection_at or owner._runner()._replay_rung_write_projection_at
            projection = project(scan)
        if projection is None or projection.scan_id != scan:
            continue
        if observation.disposition not in {"ABSENT", "STRANDED"} and advance_index is None:
            factory = question.advance_index_factory
            if factory is None:
                continue
            advance_index = factory()
        derivation = _derive_observation(
            question,
            observation,
            projection,
            epoch,
            owner,
            advance_index,
        )
        findings.append(IntrascanFinding(observation, derivation, question.source_checkpoint))
    return IntrascanResult(recorded, tuple(findings))


def _derive_observation(
    question: IntrascanQuestion,
    observation: EffectObservation,
    projection: ScanRungWriteProjection,
    epoch: Any,
    owner: Any,
    advance_index: AdvanceIndex | None,
) -> RequirementDerivation:
    """Apply the existing one-hop requirement derivations in legacy order."""

    checkpoint = question.source_checkpoint
    selected_writer = observation.obligation.producer
    if observation.disposition in {"ABSENT", "STRANDED"}:
        return _bind_guard_authority(
            question,
            derive_guard_requirement_from_effect(
                observation,
                projection,
                execution_epoch=epoch,
                execution_owner=owner,
                selected_writer=selected_writer,
                source_world_key=checkpoint.key,
                source_checkpoint=checkpoint,
                provenance="steer",
            ),
        )

    if advance_index is None:
        raise AssertionError("advance index is required for overwrite derivation")
    operand_authorities = (
        question.operand_authorities_at(projection)
        if question.operand_authorities_at is not None
        else question.operand_authorities
    )
    derivation = derive_advance_requirement_from_effect(
        advance_index,
        projection,
        observation,
        operand_authorities=operand_authorities,
        execution_epoch=epoch,
        execution_owner=owner,
        selected_writer=selected_writer,
        source_world_key=checkpoint.key,
        source_checkpoint=checkpoint,
        provenance="steer",
    )
    if derivation.requirement is not None:
        return derivation
    return _bind_guard_authority(
        question,
        derive_overwriter_guard_requirement_from_effect(
            observation,
            projection,
            execution_epoch=epoch,
            execution_owner=owner,
            selected_writer=selected_writer,
            source_world_key=checkpoint.key,
            source_checkpoint=checkpoint,
            provenance="steer-overwriter",
            preserved_values=(
                ((observation.obligation.tag, observation.obligation.value),)
                if observation.obligation.terminal_target
                else ()
            ),
        ),
    )


def _bind_guard_authority(
    question: IntrascanQuestion,
    derivation: RequirementDerivation,
) -> RequirementDerivation:
    """Classify exact guard atoms without granting execution authority."""

    requirement = bind_guard_operand_authorities(
        derivation.requirement,
        steerable=question.steerable,
        program_written=question.program_written,
        configured=question.configured_inputs,
    )
    return replace(derivation, requirement=requirement)


def build_intrascan_requirement_evidence(
    requirement: ActiveRequirement,
    projection: ScanRungWriteProjection,
    *,
    source_walk: RequirementSourceWalk | None = None,
    steerable: frozenset[str] = frozenset(),
    program_written: frozenset[str] = frozenset(),
    configured_inputs: frozenset[str] = frozenset(),
) -> IntrascanRequirementEvidence:
    """Relocate one retained requirement without retaining its old projection."""

    configured_inputs = configured_inputs | frozenset(
        getattr(requirement.source_checkpoint, "configured_inputs", frozenset())
    )
    condition = requirement.condition
    if source_walk is not None:
        if source_walk.status is not RequirementSourceWalkStatus.COMPLETE:
            return IntrascanRequirementEvidence(
                requirement,
                source_walk.condition,
                (),
                complete=False,
                detail=source_walk.detail or "same-scan requirement source walk is incomplete",
            )
        condition = source_walk.condition
    if isinstance(condition, GuardRequirementAtom | GuardRequirementExpr):
        condition = bind_guard_condition_operand_authorities(
            condition,
            steerable=steerable,
            program_written=program_written,
            configured=configured_inputs,
        )
        atoms = _guard_atoms(condition)
        targets = tuple((atom.condition, atom.deadline) for atom in atoms)
    elif isinstance(condition, Cmp):
        targets = ((condition, requirement.deadline),)
    else:
        return IntrascanRequirementEvidence(
            requirement,
            condition,
            (),
            complete=False,
            detail="requirement condition cannot be observed at one scalar occurrence",
        )

    evidence: list[IntrascanAtomEvidence] = []
    for atom_condition, deadline in targets:
        if not isinstance(atom_condition, Cmp):
            return IntrascanRequirementEvidence(
                requirement,
                condition,
                tuple(evidence),
                complete=False,
                detail="requirement atom is not an exact scalar comparison",
            )
        reads = tuple(read for read in projection.reads if occurrence_snapshot(read) == deadline)
        if len(reads) != 1:
            return IntrascanRequirementEvidence(
                requirement,
                condition,
                tuple(evidence),
                complete=False,
                detail="requirement deadline read is unavailable or ambiguous",
            )
        selector = occurrence_selector(projection, reads[0])
        if selector is None:
            return IntrascanRequirementEvidence(
                requirement,
                condition,
                tuple(evidence),
                complete=False,
                detail="requirement deadline has no relocatable occurrence selector",
            )
        evidence.append(IntrascanAtomEvidence(atom_condition, selector, deadline))
    demanding_reads = tuple(
        read
        for read in projection.reads
        if occurrence_snapshot(read) == requirement.demanding_occurrence
    )
    if len(demanding_reads) != 1:
        return IntrascanRequirementEvidence(
            requirement,
            condition,
            tuple(evidence),
            complete=False,
            detail="requirement demanding read is unavailable or ambiguous",
        )
    demanding_selector = occurrence_selector(projection, demanding_reads[0])
    if demanding_selector is None:
        return IntrascanRequirementEvidence(
            requirement,
            condition,
            tuple(evidence),
            complete=False,
            detail="requirement demanding read has no relocatable occurrence selector",
        )
    return IntrascanRequirementEvidence(
        requirement,
        condition,
        tuple(evidence),
        demanding_selector=demanding_selector,
    )


def _guard_atoms(condition: GuardRequirementCondition) -> tuple[GuardRequirementAtom, ...]:
    if isinstance(condition, GuardRequirementAtom):
        return (condition,)
    return tuple(atom for term in condition.terms for atom in _guard_atoms(term))


def observe_intrascan_requirement(
    evidence: IntrascanRequirementEvidence,
    projection: ScanRungWriteProjection,
) -> IntrascanRequirementObservation:
    """Relocate and judge one complete requirement on an exact candidate scan."""

    if not evidence.complete:
        return IntrascanRequirementObservation(
            evidence.identity,
            IntrascanRequirementDisposition.UNKNOWN,
            detail=evidence.detail or "requirement demanding occurrence is unavailable",
        )
    demanding_matches = (
        tuple(
            read
            for read in projection.reads
            if occurrence_selector(projection, read) == evidence.demanding_selector
        )
        if evidence.demanding_selector is not None
        else ()
    )
    demanding_read = demanding_matches[0] if len(demanding_matches) == 1 else None
    verdicts: list[tuple[IntrascanAtomEvidence, bool | None, Any | None]] = []
    for atom in evidence.atoms:
        matches = tuple(
            read
            for read in projection.reads
            if occurrence_selector(projection, read) == atom.selector
        )
        if len(matches) != 1 or atom.condition.bound_is_tag:
            verdicts.append((atom, None, None))
            continue
        read = matches[0]
        if read.occurrence.name != atom.condition.tag:
            verdicts.append((atom, None, read))
            continue
        verdict = constraint_holds(
            atom.condition,
            {atom.condition.tag: read.occurrence.value},
        )
        verdicts.append((atom, verdict, read))

    def atom_verdict(condition: Cmp, deadline: Any) -> bool | None:
        matching = tuple(
            verdict
            for atom, verdict, _read in verdicts
            if atom.condition == condition and atom.deadline == deadline
        )
        return matching[0] if len(matching) == 1 else None

    def evaluate(condition: ActiveCondition) -> bool | None:
        if isinstance(condition, GuardRequirementAtom):
            return (
                atom_verdict(condition.condition, condition.deadline)
                if isinstance(condition.condition, Cmp)
                else None
            )
        if isinstance(condition, GuardRequirementExpr):
            terms = tuple(evaluate(term) for term in condition.terms)
            if condition.logic.value == "all":
                if any(item is False for item in terms):
                    return False
                return True if all(item is True for item in terms) else None
            if condition.logic.value == "any":
                if any(item is True for item in terms):
                    return True
                return False if all(item is False for item in terms) else None
            return None
        if isinstance(condition, Cmp) and evidence.atoms:
            return atom_verdict(condition, evidence.atoms[0].deadline)
        return None

    logical_verdict = evaluate(evidence.condition)
    # A compound guard can decide before its final historical read.  For
    # example, the first false term of an AND prevents the remaining terms
    # from being read, while its complement already proves an ANY prevention
    # requirement.  Accept that decisive short-circuit proof only when an
    # exact relocated atom belongs to the same dynamic guard surface as the
    # demanding occurrence.  A true read from another rung/call remains
    # insufficient (and therefore UNKNOWN) when the demanding occurrence is
    # absent.
    demanding_surface = (
        (
            evidence.demanding_selector.static_address,
            evidence.demanding_selector.instruction_path,
            evidence.demanding_selector.execution_kind,
            evidence.demanding_selector.caller_rung,
            evidence.demanding_selector.call_stack,
            evidence.demanding_selector.depth,
            evidence.demanding_selector.call_invocation,
        )
        if evidence.demanding_selector is not None
        else None
    )
    exact_deciding_surface = any(
        read is not None
        and (
            atom.selector.static_address,
            atom.selector.instruction_path,
            atom.selector.execution_kind,
            atom.selector.caller_rung,
            atom.selector.call_stack,
            atom.selector.depth,
            atom.selector.call_invocation,
        )
        == demanding_surface
        for atom, _atom_verdict, read in verdicts
    )
    verdict = (
        logical_verdict
        if demanding_read is not None or (logical_verdict is not None and exact_deciding_surface)
        else None
    )
    disposition = (
        IntrascanRequirementDisposition.SATISFIED
        if verdict is True
        else IntrascanRequirementDisposition.VIOLATED
        if verdict is False
        else IntrascanRequirementDisposition.UNKNOWN
    )
    reads = [demanding_read] if demanding_read is not None else []
    reads.extend(read for _atom, _verdict, read in verdicts if read is not None)
    snapshots: list[Any] = []
    for read in reads:
        snapshot = occurrence_snapshot(read)
        if snapshot not in snapshots:
            snapshots.append(snapshot)
    return IntrascanRequirementObservation(
        evidence.identity,
        disposition,
        tuple(snapshots),
        (
            ""
            if verdict is not None
            else "demanding occurrence is unavailable or ambiguous"
            if demanding_read is None
            else "requirement deadline is unavailable or ambiguous"
        ),
    )
