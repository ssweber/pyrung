"""Report exact one-scan effect evidence without choosing or retaining a future.

The execution projection is the semantic oracle.  This module asks what one
already-executed assertion scan proves about an ``EffectExpectation`` and
interprets failed observations with the existing requirement derivations.  It
does not choose an action, install a correction, mutate PILOT state, restore or
commit a world, execute a retry, or retain navigation state.

``derive_recorded_observations`` is the compatibility seam for observations
already captured by steering.  Its filtering, projection selection, and
derivation order intentionally match the former inline implementation in
``pilot.py::_derive_attempt_requirements``.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from typing import Any

from pyrung.core.analysis.causal._rung_writes import ScanRungWriteProjection
from pyrung.core.analysis.pilot.advance import AdvanceIndex
from pyrung.core.analysis.pilot.effects import (
    EffectExpectation,
    EffectObservation,
    EffectObservationSnapshot,
    observe_execution_window,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirementSnapshot,
    FailureExplanation,
    OperandAuthority,
    RequirementDerivation,
    bind_guard_operand_authorities,
    derive_advance_requirement_from_effect,
    derive_guard_requirement_from_effect,
    derive_overwriter_guard_requirement_from_effect,
)


@dataclass(frozen=True)
class IntrascanQuestion:
    """Evidence and authority needed to inspect one exact assertion scan."""

    expectation: EffectExpectation | None
    execution: Any = field(compare=False, repr=False)
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

    def diagnostic_snapshot(self) -> IntrascanFindingSnapshot:
        """Detach observation, derivation, source, and causal identity evidence."""

        checkpoint_work = getattr(
            getattr(self.source_checkpoint, "world", None),
            "work",
            None,
        )
        requirement = self.derivation.requirement
        return IntrascanFindingSnapshot(
            observation=self.observation.diagnostic_snapshot(),
            explanation=self.derivation.explanation,
            requirement=(requirement.diagnostic_snapshot() if requirement is not None else None),
            selected_writer=self.observation.obligation.producer,
            source_world_key=self.source_checkpoint.key,
            source_scan=getattr(getattr(checkpoint_work, "state", None), "scan_id", None),
            causal_identity=(
                id(self.observation.execution_epoch),
                id(self.observation.execution_owner),
                id(self.source_checkpoint.owner),
            ),
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
    causal_identity: tuple[int, int, int]


@dataclass(frozen=True)
class IntrascanResult:
    """Factual observations and derivations for one report-only question."""

    observations: tuple[EffectObservation, ...] = field(compare=False, repr=False)
    findings: tuple[IntrascanFinding, ...]

    def diagnostic_snapshot(self) -> tuple[IntrascanFindingSnapshot, ...]:
        """Return complete detached findings in legacy receipt order."""

        return tuple(finding.diagnostic_snapshot() for finding in self.findings)


def inspect_assertion_scan(question: IntrascanQuestion) -> IntrascanResult:
    """Inspect exactly the assertion scan named by ``question``.

    ``observe_execution_window`` owns scan selection and delegates the actual
    producer/consumer interpretation to ``observe_expectation``.  Missing or
    ambiguous projection evidence therefore remains ``UNKNOWN`` and produces
    no derived finding.
    """

    project = question.projection_at or question.execution._replay_rung_write_projection_at

    def exact_projection_at(scan_id: int) -> ScanRungWriteProjection | None:
        projection = project(scan_id)
        if projection is None or projection.scan_id != scan_id:
            return None
        return projection

    observations = observe_execution_window(
        question.expectation,
        question.execution,
        scan_before=question.assertion_scan - 1,
        kernel_scan_ids=(question.assertion_scan,),
        action_scan=question.assertion_scan,
        projection_at=exact_projection_at,
    )
    return derive_recorded_observations(question, observations)


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
            observation.disposition == "SURVIVED"
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
