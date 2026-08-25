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
    occurrence_selector,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.execution import CheckpointRef
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
from pyrung.core.crossing import Cmp
from pyrung.core.instruction.advance import constraint_holds
from pyrung.core.runner import EpochRef


@dataclass(frozen=True)
class IntrascanQuestion:
    """Derivation authority for already-recorded intrascan observations."""

    source_checkpoint: Any = field(compare=False, repr=False)
    advance_index: AdvanceIndex | None = field(compare=False, repr=False)
    operand_authorities: Mapping[str, OperandAuthority] = field(compare=False, repr=False)
    steerable: frozenset[str]
    program_written: frozenset[str]
    configured_inputs: frozenset[str] = frozenset()
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


def derive_recorded_observations(
    question: IntrascanQuestion,
    observations: Sequence[EffectObservation],
    *,
    fallback_scan: int,
    projection_at: Callable[[int], ScanRungWriteProjection | None],
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
        if owner is None or observation.execution_epoch is None:
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
        scan = max(scans, default=fallback_scan)
        projection = observation.execution_projection
        if projection is None or projection.scan_id != scan:
            projection = projection_at(scan)
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
            owner,
            advance_index,
        )
        findings.append(IntrascanFinding(observation, derivation, question.source_checkpoint))
    return IntrascanResult(recorded, tuple(findings))


def _derive_observation(
    question: IntrascanQuestion,
    observation: EffectObservation,
    projection: ScanRungWriteProjection,
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
