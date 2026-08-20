"""Apply accepted evidence and lifecycle facts to durable WorkingTheory state.

This module is the sole mutable application seam for opening, refining,
recording, advancing, proving, and abandoning the technician's persistent
case. It never restores a World, composes executable configuration, chooses a
Bearing, executes one, or runs the outer Pilot loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.attempt_interpretation import (
    AttemptInterpretationKind,
    interpret_exact_regression_requirement,
    interpret_failed_requirements,
)
from pyrung.core.analysis.pilot.bootstrap import _BootstrapExecution
from pyrung.core.analysis.pilot.effects import ConsumerBoundary
from pyrung.core.analysis.pilot.execution import (
    PulseHorizon,
    ScanEntryConfiguration,
    execution_owner,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    BearingObjective,
    LocalProgressKind,
    _ActionPair,
    act_identity,
)
from pyrung.core.analysis.pilot.requirement_evidence import _exact_failed_source
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    FailedEffectReceipt,
    RequirementStatus,
)
from pyrung.core.analysis.pilot.theory_evidence import (
    _execution_consumer_boundary,
    _theory_bootstrap_transition,
    _theory_boundary_from_checkpoint,
    _theory_claim,
    _theory_live_boundary,
    _theory_regression_claim,
    _theory_requirement_snapshot,
    _TheoryTransitionEvidence,
)
from pyrung.core.analysis.pilot.theory_reducer import (
    AbandonTheory,
    AdvanceTheory,
    OpenTheory,
    ProveTheory,
    RecordTheoryAttempt,
    RefineTheory,
    reduce_theory,
)
from pyrung.core.analysis.pilot.types import (
    _AcceptedTrial,
    _AttemptResult,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import (
    IntrascanTracebackFinding,
    ProgramTransaction,
    TemporalNeedRequest,
    TheoryAttemptDisposition,
    TheoryAttemptReceipt,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryPhaseKind,
    TheoryPhaseReceipt,
    TheoryTemporalIntent,
    TheoryTermination,
    active_theory,
    theory_source_is_retained,
    theory_view,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key

if TYPE_CHECKING:
    from pyrung.core.runner import EpochRef

logger = logging.getLogger(__name__)


def _theory_transition_from_failed_requirements(
    state: _PilotState,
    exact_pairs: tuple[tuple[ActiveRequirement, FailedEffectReceipt], ...],
    *,
    assertion_scan: int,
    evidence: tuple[Any, ...],
    landing_owns_tip: bool = True,
) -> _TheoryTransitionEvidence | None:
    """Build one detached controlling transition from exact failed requirements.

    The caller owns how the failure was discovered.  This adapter admits only
    one transaction, one expectation, and one executable requirement source;
    it neither mutates the ledger nor restores that source.
    """

    if not exact_pairs:
        return None
    failed_receipts = tuple(failed for _requirement, failed in exact_pairs)
    if (
        len({failed.act_identity for failed in failed_receipts}) != 1
        or len({id(failed.expectation) for failed in failed_receipts}) != 1
        or len({id(failed.local_bearing) for failed in failed_receipts}) != 1
        or len({failed.checkpoint_ref for failed in failed_receipts}) != 1
    ):
        return None
    failed = failed_receipts[0]
    if failed.expectation is None or failed.local_bearing is None:
        return None
    source_checkpoints = {
        requirement.checkpoint_ref: requirement.source_checkpoint
        for requirement, _failed in exact_pairs
    }
    if len(source_checkpoints) != 1:
        return None
    checkpoint = next(iter(source_checkpoints.values()))
    source = _theory_boundary_from_checkpoint(checkpoint)
    interpretation = interpret_failed_requirements(
        exact_pairs=exact_pairs,
        assertion_scan=assertion_scan,
        landing_owns_tip=landing_owns_tip,
    )
    if not interpretation.opens_theory:
        return None
    return _TheoryTransitionEvidence(
        claim=_theory_claim(failed.expectation, failed.local_bearing.objective, source),
        source=source,
        execution_ref=failed.execution_ref,
        occurrence_evidence=tuple(_semantic_key(item.explanation) for item in failed_receipts),
        act_identity=failed.act_identity,
        act_pairs=tuple(failed.local_bearing.act.policy.applied),
        selected_act_pairs=tuple(failed.local_bearing.act.policy.action_pairs),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=TheoryAttemptDisposition.REJECTED_EXACT,
        evidence=evidence,
        requirements=tuple(
            _theory_requirement_snapshot(requirement) for requirement, _failed in exact_pairs
        ),
        interpretation=interpretation,
        conductivity_observations=tuple(item.observation for item in failed_receipts),
    )


def _record_theory_from_failed_requirements(
    state: _PilotState,
    exact_pairs: tuple[tuple[ActiveRequirement, FailedEffectReceipt], ...],
    *,
    assertion_scan: int,
    evidence: tuple[Any, ...],
    remaining_budget: int,
    landing_owns_tip: bool = True,
) -> _TheoryTransitionEvidence | None:
    """Admit one exact failed transaction to the controlling theory lifecycle."""

    transition = _theory_transition_from_failed_requirements(
        state,
        exact_pairs,
        assertion_scan=assertion_scan,
        evidence=evidence,
        landing_owns_tip=landing_owns_tip,
    )
    if transition is None:
        return None
    before = state.theory_state
    _record_theory_transition(
        state,
        transition,
        remaining_budget=remaining_budget,
        record_fact=_record_controlling_theory_fact,
    )
    return transition if state.theory_state != before else None


def _theory_transition_from_regression_requirement(
    state: _PilotState,
    requirement: ActiveRequirement,
    bearing: Bearing,
    *,
    evidence: tuple[Any, ...],
) -> _TheoryTransitionEvidence | None:
    """Detach one exact corrective obstruction for the common theory reducer."""

    obstruction = requirement.obstruction_occurrence
    if obstruction is None:
        return None
    source = _theory_boundary_from_checkpoint(requirement.source_checkpoint)
    interpretation = interpret_exact_regression_requirement(
        requirement,
        assertion_scan=obstruction.scan_id,
    )
    if not interpretation.opens_theory:
        return None
    return _TheoryTransitionEvidence(
        claim=_theory_regression_claim(bearing.objective, source, obstruction),
        source=source,
        execution_ref=requirement.execution_ref,
        occurrence_evidence=(("regression-obstruction", _semantic_key(obstruction)),),
        act_identity=act_identity(bearing.act),
        act_pairs=tuple(bearing.act.policy.applied),
        selected_act_pairs=tuple(bearing.act.policy.action_pairs),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=TheoryAttemptDisposition.REJECTED_EXACT,
        evidence=evidence,
        requirements=(_theory_requirement_snapshot(requirement),),
        interpretation=interpretation,
    )


def _record_theory_from_regression_requirement(
    state: _PilotState,
    requirement: ActiveRequirement,
    bearing: Bearing,
    *,
    evidence: tuple[Any, ...],
    remaining_budget: int,
) -> _TheoryTransitionEvidence | None:
    """Admit one exact causal obstruction without private counterfactual replay."""

    transition = _theory_transition_from_regression_requirement(
        state,
        requirement,
        bearing,
        evidence=evidence,
    )
    if transition is None:
        return None
    before = state.theory_state
    _record_theory_transition(
        state,
        transition,
        remaining_budget=remaining_budget,
        record_fact=_record_controlling_theory_fact,
    )
    return transition if state.theory_state != before else None


def _advance_theory_to_regression_prefix(
    state: _PilotState,
    trial: _AcceptedTrial,
    checkpoint: _CausalCheckpoint,
    obstruction: Any,
) -> bool:
    """Receipt the clean part of an accepted act before one exact obstruction.

    The admitted tentative-rung proof owns the bounded scan pipeline between
    ``boundary`` and ``obstruction``.  This adapter only advances the theory to
    the executable source of that proof; it must not re-impose one-scan
    adjacency here.
    """

    theory = active_theory(state.theory_state)
    if theory is None:
        return False
    source = state.theory_state.ledger.progress[theory.current_progress_id].provisional_tip
    boundary = _theory_boundary_from_checkpoint(checkpoint)
    if theory_source_is_retained(state.theory_state, boundary):
        return True
    execution = trial.execution
    owner = execution.owner_at(boundary.scan_id)
    if (
        trial.attempt.pulse.scan_before != source.scan_id
        or boundary.scan_id <= source.scan_id
        or boundary.scan_id >= obstruction.scan_id
        or owner is None
        or owner.epoch.reference != boundary.execution_ref
    ):
        return False
    evidence = (
        "accepted-prefix-before-obstruction",
        source,
        boundary,
        _semantic_key(obstruction),
    )
    attempt_id = (
        "regression-prefix-attempt",
        theory.theory_id,
        theory.current_version_id,
        evidence,
    )
    _record_controlling_theory_fact(
        state,
        RecordTheoryAttempt(
            TheoryAttemptReceipt(
                theory_id=theory.theory_id,
                version_id=theory.current_version_id,
                attempt_id=attempt_id,
                source=source,
                execution_ref=owner.epoch.reference,
                occurrence_evidence=(evidence,),
                act_identity=act_identity(trial.attempt.bearing.act),
                act_pairs=(),
                selected_act_pairs=tuple(trial.attempt.bearing.act.policy.action_pairs),
                pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
                disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
                evidence=(evidence,),
                execution_source=source,
                observation_boundary=source,
                program_transaction=ProgramTransaction.from_heading(
                    trial.attempt.bearing.act.policy.heading,
                    execution.before_snap,
                ),
            ),
        ),
    )
    _record_controlling_theory_fact(
        state,
        AdvanceTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            accepted_attempt_id=attempt_id,
            source=source,
            boundary=boundary,
            advance_identity=("regression-prefix-advance", attempt_id, boundary),
            phase_receipts=(
                TheoryPhaseReceipt(
                    kind=TheoryPhaseKind.SCAN_PROGRESS,
                    evidence_identity=(evidence,),
                ),
            ),
            execution_source=source,
        ),
    )
    return theory_source_is_retained(state.theory_state, boundary)


def _open_theory_from_program_guard_rebases(
    state: _PilotState,
    rebases: tuple[tuple[ActiveRequirement, ActiveRequirement], ...],
    *,
    remaining_budget: int,
) -> bool:
    """Open one controlling theory from an explained failed act plus rebased facts."""

    exact_pairs_list: list[tuple[ActiveRequirement, FailedEffectReceipt]] = []
    for parent, rebased in rebases:
        failed = _exact_failed_source(parent, state)
        if failed is None:
            matches = tuple(
                receipt
                for receipt in state.failed_effect_receipts
                if receipt.source_checkpoint.owner is parent.source_checkpoint.owner
                and receipt.source_world_key == parent.source_world_key
            )
            failed = matches[0] if len(matches) == 1 else None
        if failed is not None:
            exact_pairs_list.append((rebased, failed))
    exact_pairs = tuple(exact_pairs_list)
    if not exact_pairs:
        return False
    transition = _record_theory_from_failed_requirements(
        state,
        exact_pairs=exact_pairs,
        assertion_scan=max(requirement.deadline.scan_id for requirement, _ in exact_pairs),
        evidence=(
            ("program-guard-rebases", tuple(item.navigation_identity for _, item in rebases)),
        ),
        remaining_budget=remaining_budget,
    )
    if transition is None:
        return False
    return active_theory(state.theory_state) is not None


def _refine_active_theory_from_program_guard_rebases(
    state: _PilotState,
    rebases: tuple[tuple[ActiveRequirement, ActiveRequirement], ...],
) -> bool:
    """Turn an exact earlier prevention fact into the next temporal request.

    The failed act belongs to the active provisional tip, while the rebased
    requirement belongs to the retained checkpoint before its harmful writer.
    Record both boundaries explicitly: the former owns the rejected-attempt
    evidence and the latter is where Compass must read the SETUP_FIRST bearing.
    """

    theory = active_theory(state.theory_state)
    if theory is None:
        return False
    exact_pairs: list[tuple[ActiveRequirement, FailedEffectReceipt]] = []
    for parent, rebased in rebases:
        failed = _exact_failed_source(parent, state)
        if failed is None:
            matches = tuple(
                receipt
                for receipt in state.failed_effect_receipts
                if receipt.source_checkpoint.owner is parent.source_checkpoint.owner
                and receipt.source_world_key == parent.source_world_key
            )
            failed = matches[0] if len(matches) == 1 else None
        if failed is not None:
            exact_pairs.append((rebased, failed))
    if not exact_pairs:
        return False
    failed_receipts = tuple(failed for _requirement, failed in exact_pairs)
    if (
        len({failed.act_identity for failed in failed_receipts}) != 1
        or len({id(failed.local_bearing) for failed in failed_receipts}) != 1
        or len({failed.checkpoint_ref for failed in failed_receipts}) != 1
    ):
        return False
    progress = state.theory_state.ledger.progress[theory.current_progress_id]
    trigger_source = progress.provisional_tip
    failed_source = _theory_boundary_from_checkpoint(failed_receipts[0].source_checkpoint)
    if failed_source != trigger_source:
        return False
    refined_sources = {
        _theory_boundary_from_checkpoint(requirement.source_checkpoint)
        for requirement, _failed in exact_pairs
    }
    if len(refined_sources) != 1:
        return False
    refined_source = next(iter(refined_sources))
    interpretation = interpret_failed_requirements(
        exact_pairs=tuple(exact_pairs),
        assertion_scan=max(requirement.deadline.scan_id for requirement, _ in exact_pairs),
        landing_owns_tip=False,
    )
    if not interpretation.opens_theory:
        return False
    attempt_identity = (
        "program-guard-rebase-attempt",
        theory.theory_id,
        theory.current_version_id,
        trigger_source,
        tuple(requirement.navigation_identity for requirement, _failed in exact_pairs),
    )
    failed = failed_receipts[0]
    _record_controlling_theory_fact(
        state,
        RecordTheoryAttempt(
            TheoryAttemptReceipt(
                theory_id=theory.theory_id,
                version_id=theory.current_version_id,
                attempt_id=attempt_identity,
                source=trigger_source,
                execution_ref=failed.execution_ref,
                occurrence_evidence=tuple(
                    _semantic_key(item.explanation) for item in failed_receipts
                ),
                act_identity=failed.act_identity,
                act_pairs=tuple(failed.local_bearing.act.policy.applied),
                selected_act_pairs=tuple(failed.local_bearing.act.policy.action_pairs),
                pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
                disposition=TheoryAttemptDisposition.REJECTED_EXACT,
                evidence=(
                    (
                        "program-guard-rebases",
                        tuple(
                            requirement.navigation_identity for requirement, _failed in exact_pairs
                        ),
                    ),
                ),
                conductivity_observations=tuple(item.observation for item in failed_receipts),
                observation_boundary=trigger_source,
            ),
        ),
    )
    theory = active_theory(state.theory_state)
    assert theory is not None
    _record_controlling_theory_fact(
        state,
        RefineTheory(
            theory_id=theory.theory_id,
            parent_version_id=theory.current_version_id,
            source=trigger_source,
            refined_source=refined_source,
            requirements=tuple(
                _theory_requirement_snapshot(requirement) for requirement, _failed in exact_pairs
            ),
            refinement_identity=(
                "program-guard-rebase",
                attempt_identity,
                refined_source,
            ),
            temporal_intent=TheoryTemporalIntent(interpretation.kind.value),
            trigger_attempt_id=attempt_identity,
            temporal_source=refined_source,
        ),
    )
    return True


def _record_optional_theory_fact(state: _PilotState, fact: Any) -> None:
    """Record a non-controlling lifecycle fact through one fail-open seam."""

    try:
        state.theory_state = reduce_theory(state.theory_state, fact)
    except Exception:  # noqa: BLE001 - optional theory recording cannot change the drive
        logger.debug("pilot: optional theory reduction failed", exc_info=True)


def _record_controlling_theory_fact(state: _PilotState, fact: Any) -> None:
    """Apply a fact which production control now depends on; fail closed loudly."""

    state.theory_state = reduce_theory(state.theory_state, fact)


def _run_optional_theory_hook(hook: Callable[..., None], *args: Any, **kwargs: Any) -> None:
    """Keep every optional theory conversion outside production control flow."""

    try:
        hook(*args, **kwargs)
    except Exception:  # noqa: BLE001 - optional theory recording cannot change the drive
        logger.debug("pilot: optional theory hook failed", exc_info=True)


def _requirement_identities(state: Any) -> frozenset[tuple[Any, ...]]:
    """Read the exact live requirement set used by monitor control."""

    return frozenset(requirement.identity for requirement in state.active_requirements)


def _theory_claim_correlates(state: _PilotState, claim: TheoryClaim) -> bool:
    theory = active_theory(state.theory_state)
    if theory is None:
        return True
    active_claim = state.theory_state.ledger.claims[theory.claim_id]
    same_target = (
        claim.objective.target_tag == active_claim.objective.target_tag
        and claim.objective.target_value == active_claim.objective.target_value
        and claim.objective.predicate_identity == active_claim.objective.predicate_identity
    )
    return same_target and theory_source_is_retained(state.theory_state, claim.source)


def _theory_attempt_identity(
    theory_id: tuple[Any, ...],
    observation: _TheoryTransitionEvidence,
) -> tuple[Any, ...]:
    return (
        "theory-attempt",
        theory_id,
        observation.identity,
        observation.execution_ref,
        observation.occurrence_evidence,
    )


def _record_theory_transition(
    state: _PilotState,
    observation: _TheoryTransitionEvidence | None,
    *,
    remaining_budget: int,
    record_fact: Callable[[_PilotState, Any], None],
) -> None:
    if observation is None:
        return
    if state.theory_state.active_theory_id is None:
        if not observation.interpretation.opens_theory:
            return
        record_fact(
            state,
            OpenTheory(
                claim=observation.claim,
                opening_identity=("theory-open", observation.claim.identity),
                remaining_budget=max(0, remaining_budget),
            ),
        )
    elif not _theory_claim_correlates(state, observation.claim):
        return

    theory = active_theory(state.theory_state)
    if theory is None:
        return
    attempt_identity = _theory_attempt_identity(theory.theory_id, observation)
    record_fact(
        state,
        RecordTheoryAttempt(
            TheoryAttemptReceipt(
                theory_id=theory.theory_id,
                version_id=theory.current_version_id,
                attempt_id=attempt_identity,
                source=observation.source,
                execution_ref=observation.execution_ref,
                occurrence_evidence=observation.occurrence_evidence,
                act_identity=observation.act_identity,
                act_pairs=observation.act_pairs,
                selected_act_pairs=observation.selected_act_pairs,
                pilot_rung_identities=observation.pilot_rung_identities,
                disposition=observation.disposition,
                evidence=observation.evidence,
                conductivity_observations=observation.conductivity_observations,
                consumer_boundary=observation.consumer_boundary,
                investigation_frontier_id=observation.investigation_frontier_id,
                producer_goal_id=observation.producer_goal_id,
                observation_boundary=observation.observation_boundary or observation.source,
                program_transaction=observation.program_transaction,
                configurations=observation.configurations,
            ),
        ),
    )
    if observation.requirements:
        theory = active_theory(state.theory_state)
        assert theory is not None
        controlling_intent = (
            TheoryTemporalIntent(observation.interpretation.kind.value)
            if observation.interpretation.opens_theory
            and observation.disposition is TheoryAttemptDisposition.REJECTED_EXACT
            else None
        )
        record_fact(
            state,
            RefineTheory(
                theory_id=theory.theory_id,
                parent_version_id=theory.current_version_id,
                source=observation.source,
                # Intrascan refinement changes what must be present while the
                # triggering scan executes. A later settled landing can be the
                # regression being explained, so it is not the executable
                # source for the retry.
                refined_source=observation.source,
                requirements=observation.requirements,
                refinement_identity=(
                    "theory-refine",
                    observation.identity,
                    tuple(item.semantic_identity for item in observation.requirements),
                ),
                temporal_intent=controlling_intent,
                trigger_attempt_id=(attempt_identity if controlling_intent is not None else None),
                temporal_source=(observation.source if controlling_intent is not None else None),
            ),
        )


def _record_working_theory_transition(
    state: _PilotState,
    observation: _TheoryTransitionEvidence | None,
    *,
    remaining_budget: int,
) -> None:
    if _records_controlling_need(observation):
        assert observation is not None
        _record_controlling_transition(
            state,
            observation,
            remaining_budget=remaining_budget,
        )
    else:
        _record_theory_transition(
            state,
            observation,
            remaining_budget=remaining_budget,
            record_fact=_record_optional_theory_fact,
        )


def _record_controlling_transition(
    state: _PilotState,
    observation: _TheoryTransitionEvidence,
    *,
    remaining_budget: int,
) -> None:
    """Establish one exact controlling theory without a fail-open recorder seam."""

    if observation.disposition is not TheoryAttemptDisposition.REJECTED_EXACT:
        raise ValueError("controlling transition lacks an exact rejected attempt")
    if observation.interpretation.kind not in {
        AttemptInterpretationKind.SETUP_FIRST,
        AttemptInterpretationKind.RETRY_TOGETHER,
        AttemptInterpretationKind.RETRY_THROUGH_DEADLINE,
    }:
        raise ValueError("controlling transition lacks an actionable temporal need")
    _record_theory_transition(
        state,
        observation,
        remaining_budget=remaining_budget,
        record_fact=_record_controlling_theory_fact,
    )


def _records_controlling_need(observation: _TheoryTransitionEvidence | None) -> bool:
    return bool(
        observation is not None
        and observation.disposition is TheoryAttemptDisposition.REJECTED_EXACT
        and observation.interpretation.kind
        in {
            AttemptInterpretationKind.SETUP_FIRST,
            AttemptInterpretationKind.RETRY_TOGETHER,
            AttemptInterpretationKind.RETRY_THROUGH_DEADLINE,
        }
        and observation.requirements
    )


@dataclass(frozen=True)
class _ControlledSetupAttempt:
    request: TemporalNeedRequest
    attempt_id: tuple[Any, ...]
    execution_ref: EpochRef
    occurrence_evidence: tuple[Any, ...]
    act_identity: tuple[Any, ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    local_requirement_identities: tuple[tuple[Any, ...], ...]
    setup_pairs: tuple[_ActionPair, ...]
    executed_pending_overlay_identities: tuple[tuple[Any, ...], ...]
    configurations: tuple[ScanEntryConfiguration, ...]
    phase: str
    objective: BearingObjective
    execution_source: TheoryBoundaryIdentity
    consumer_boundary: ConsumerBoundary | None = None
    consumer_boundary_reached: bool | None = None


def _record_controlled_setup_attempt(
    state: _PilotState,
    request: TemporalNeedRequest,
    bearing: Bearing,
    attempt: _AttemptResult,
    source_checkpoint: _CausalCheckpoint,
) -> _ControlledSetupAttempt:
    """Record one ordinary setup execution before its fork may be adopted."""

    execution = attempt.executed_attempt
    if execution is None:
        raise ValueError("setup-first execution lost its exact attempt evidence")
    execution_receipt = execution.execution
    if execution_receipt is None:
        raise ValueError("setup-first execution has no immutable execution receipt")
    owner = execution_owner(execution.pulse.fork, execution.assertion_scan)
    if owner is None:
        raise ValueError("setup-first execution has no exact assertion owner")
    projection = execution.projection_at(execution.assertion_scan)
    if projection is None:
        raise ValueError("setup-first execution has no owner-bound projection")
    occurrences = tuple(
        (
            "write",
            write.scan_id,
            write.ordinal,
            write.transition.tag_name,
            _semantic_key(write.transition.from_value),
            _semantic_key(write.transition.to_value),
        )
        for write in projection.writes
    )
    occurrence_evidence = ("setup-assertion-scan", execution.assertion_scan, occurrences)
    execution_source = _theory_boundary_from_checkpoint(source_checkpoint)
    execution_ref = execution_receipt.epoch_ref
    action_identity = act_identity(bearing.act)
    local_sources = (
        bearing.act.policy.local_progress_sources or bearing.act.policy.local_progress_requirements
    )
    local_requirement_identities = tuple(
        _theory_requirement_snapshot(requirement).semantic_identity for requirement in local_sources
    )
    phase = (
        "rearm"
        if bearing.act.policy.local_progress is LocalProgressKind.REARM
        else "transaction"
        if bearing.act.policy.local_progress is LocalProgressKind.TEMPORAL_EDGE
        else "correction"
        if bearing.act.policy.local_progress is LocalProgressKind.THEORY_CORRECTIVE
        else "need"
    )
    rung_identities = tuple(_rung_identity(rung) for rung in state.pilot_rungs)
    view = theory_view(state.theory_state)
    pending = view.pending_overlay_identities if view is not None else frozenset()
    executed_pending = tuple(
        _rung_identity(rung) for rung in state.pilot_rungs if _rung_identity(rung) in pending
    )
    attempt_id = (
        "working-theory-setup",
        request.theory_id,
        request.version_id,
        request.source,
        request.trigger_attempt_id,
        tuple(item.semantic_identity for item in request.requirements),
        ("execution-source-scan", execution.pulse.scan_before),
        action_identity,
        rung_identities,
    )
    disposition = (
        TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
        if attempt.trial is not None
        else TheoryAttemptDisposition.REJECTED_EXACT
        if attempt.proof_rejection
        else TheoryAttemptDisposition.REJECTED_EMPIRICAL
    )
    _record_controlling_theory_fact(
        state,
        RecordTheoryAttempt(
            TheoryAttemptReceipt(
                theory_id=request.theory_id,
                version_id=request.version_id,
                attempt_id=attempt_id,
                source=request.source,
                execution_ref=execution_ref,
                occurrence_evidence=occurrence_evidence,
                act_identity=action_identity,
                act_pairs=tuple(bearing.act.policy.applied),
                selected_act_pairs=tuple(bearing.act.policy.action_pairs),
                pilot_rung_identities=rung_identities,
                disposition=disposition,
                evidence=(
                    (
                        "temporal-phase",
                        phase,
                        tuple(item.semantic_identity for item in request.requirements),
                    ),
                ),
                consumer_boundary=_execution_consumer_boundary(execution),
                execution_source=execution_source,
                observation_boundary=request.source,
                program_transaction=ProgramTransaction.from_heading(
                    bearing.act.policy.heading,
                    dict(source_checkpoint.world.work.state.tags),
                ),
                configurations=execution_receipt.applied_configurations,
            ),
        ),
    )
    return _ControlledSetupAttempt(
        request,
        attempt_id,
        execution_ref,
        occurrence_evidence,
        action_identity,
        rung_identities,
        local_requirement_identities,
        tuple(bearing.act.policy.applied),
        executed_pending,
        execution_receipt.applied_configurations,
        phase,
        bearing.objective,
        execution_source,
        _execution_consumer_boundary(execution),
        execution_receipt.consumer_boundary_reached,
    )


def _complete_intrascan_consumer(
    state: _PilotState,
    request: TemporalNeedRequest | None,
    trial: _AcceptedTrial,
    observation: _TheoryTransitionEvidence | None,
) -> None:
    """Close only the temporal question proved by one exact consumer scan."""

    receipt = trial.execution.intrascan_act
    if receipt is None or receipt.kind != "consumer":
        return
    if request is None or observation is None:
        raise ValueError("accepted intrascan consumer lost its temporal ownership")
    if observation.disposition is not TheoryAttemptDisposition.ACCEPTED_PROVISIONAL:
        raise ValueError("intrascan consumer completion requires an accepted attempt")
    theory = active_theory(state.theory_state)
    if theory is None or theory.theory_id != request.theory_id:
        raise ValueError("accepted intrascan consumer lost its active theory")
    finding = state.theory_state.ledger.traceback_findings.get(receipt.evidence_identity)
    if (
        finding is None
        or not isinstance(finding, IntrascanTracebackFinding)
        or finding.theory_id != theory.theory_id
        or finding.version_id != request.version_id
        or finding.source != observation.source
        or finding.realization.consumer_write is None
        or receipt.expected_write_identity != _semantic_key(finding.realization.consumer_write)
    ):
        raise ValueError("intrascan consumer receipt does not answer its finding")
    attempt_id = _theory_attempt_identity(theory.theory_id, observation)
    progress = state.theory_state.ledger.progress[theory.current_progress_id]
    if progress.accepted_attempt_id != attempt_id:
        raise ValueError("intrascan consumer did not own the advanced theory World")

    finding_requirements = set(finding.requirement_identities)
    parent_frontier_id = finding.parent_frontier_id
    seen_frontiers: set[tuple[Any, ...]] = set()
    while parent_frontier_id is not None:
        if parent_frontier_id in seen_frontiers:
            raise ValueError("intrascan consumer finding has cyclic frontier ancestry")
        seen_frontiers.add(parent_frontier_id)
        parent = state.theory_state.ledger.traceback_frontiers.get(parent_frontier_id)
        if parent is None or parent.theory_id != theory.theory_id:
            raise ValueError("intrascan consumer finding lost frontier ancestry")
        finding_requirements.update(parent.requirement_identities)
        parent_frontier_id = parent.parent_frontier_id
    request_identities = frozenset(item.semantic_identity for item in request.requirements)
    if not request_identities <= finding_requirements:
        raise ValueError("intrascan consumer finding does not own its temporal request")
    discharged = tuple(
        requirement
        for requirement in state.active_requirements
        if requirement.status is RequirementStatus.ACTIVE
        if _theory_requirement_snapshot(requirement).semantic_identity in finding_requirements
    )
    if not discharged:
        raise ValueError("intrascan consumer finding owns no live temporal requirement")
    for requirement in discharged:
        index = state.active_requirements.index(requirement)
        state.active_requirements[index] = replace(
            requirement,
            status=RequirementStatus.DISCHARGED,
        )

    if any(
        requirement.status is RequirementStatus.ACTIVE
        and _theory_requirement_snapshot(requirement).semantic_identity in request_identities
        for requirement in state.active_requirements
    ):
        return
    boundary = _theory_live_boundary(state)
    theory = active_theory(state.theory_state)
    if theory is None:
        raise ValueError("intrascan consumer completion lost its active theory")
    _record_controlling_theory_fact(
        state,
        RefineTheory(
            theory_id=theory.theory_id,
            parent_version_id=theory.current_version_id,
            source=boundary,
            refined_source=boundary,
            requirements=(),
            refinement_identity=(
                "working-theory-intrascan-consumer-yield",
                receipt.evidence_identity,
                attempt_id,
            ),
        ),
    )


def _record_bootstrap_theory_transition(
    state: _PilotState,
    ctx: _PilotContext,
    receipt: _BootstrapExecution | None,
    *,
    remaining_budget: int,
) -> None:
    if receipt is None:
        return
    _record_working_theory_transition(
        state,
        _theory_bootstrap_transition(state, ctx, receipt),
        remaining_budget=remaining_budget,
    )


def _record_optional_requirement_delta(
    state: _PilotState,
    before: frozenset[tuple[Any, ...]],
    *,
    identity: tuple[Any, ...],
    record_fact: Callable[[_PilotState, Any], None] = _record_optional_theory_fact,
) -> None:
    theory = active_theory(state.theory_state)
    if theory is None:
        return
    novel = tuple(
        _theory_requirement_snapshot(requirement)
        for requirement in state.active_requirements
        if requirement.identity not in before
    )
    if not novel:
        return
    record_fact(
        state,
        RefineTheory(
            theory_id=theory.theory_id,
            parent_version_id=theory.current_version_id,
            source=state.theory_state.ledger.progress[theory.current_progress_id].provisional_tip,
            refined_source=_theory_live_boundary(state),
            requirements=novel,
            refinement_identity=("theory-refine", identity),
        ),
    )


def _record_optional_theory_proved(state: _PilotState) -> None:
    theory = active_theory(state.theory_state)
    if theory is None:
        return
    boundary = _theory_live_boundary(state)
    unresolved = tuple(
        requirement
        for requirement in state.active_requirements
        if requirement.status is RequirementStatus.ACTIVE
    )
    if unresolved:
        return
    attempts = tuple(
        state.theory_state.ledger.attempts[attempt_id]
        for attempt_id in theory.attempt_ids
        if state.theory_state.ledger.attempts[attempt_id].disposition
        in (
            TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
            TheoryAttemptDisposition.WITNESS,
        )
    )
    if not attempts:
        return
    _record_optional_theory_fact(
        state,
        ProveTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            proof_identity=("theory-proved", theory.theory_id, boundary),
        ),
    )


def _record_optional_theory_abandoned(
    state: _PilotState,
    termination: TheoryTermination,
) -> None:
    theory = active_theory(state.theory_state)
    if theory is None:
        return
    _record_optional_theory_fact(
        state,
        AbandonTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            termination=termination,
            abandonment_identity=(
                "theory-abandoned",
                theory.theory_id,
                theory.current_version_id,
                termination,
                _theory_live_boundary(state),
            ),
        ),
    )


def _record_theory_execution_advance(
    state: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    observation: _TheoryTransitionEvidence | None,
) -> None:
    """Advance theory progress and any receipt-owned consumer horizon."""

    theory = active_theory(state.theory_state)
    receipt = trial.execution.scan_progress
    if theory is None:
        return
    progress = state.theory_state.ledger.progress[theory.current_progress_id]
    source = progress.provisional_tip
    boundary = _theory_live_boundary(state)
    current_view = theory_view(state.theory_state)
    current_scope = current_view.investigation_scope if current_view is not None else None
    policy = trial.attempt.bearing.act.policy
    contiguous_progress = bool(
        receipt is None
        and current_scope is not None
        and current_scope.transaction_attempt_id is not None
        and current_scope.consumer_boundary is not None
        and policy.motion.is_coast
        and not policy.applied
        and trial.attempt.pulse.scan_before == source.scan_id
        and trial.attempt.pulse.fork.state.scan_id == boundary.scan_id
        and dict(state.work.state.tags) == dict(trial.execution.after_snap)
    )
    if (
        boundary.scan_id <= source.scan_id
        or (receipt is not None and receipt.source_scan != source.scan_id)
        or (receipt is None and not contiguous_progress)
    ):
        return
    consumer_boundary = _execution_consumer_boundary(trial.attempt)
    attempted_act_identity = act_identity(trial.attempt.bearing.act)
    advance_evidence = (
        ("scan-progress", _semantic_key(receipt))
        if receipt is not None
        else (
            "accepted-consumer-continuation",
            observation.identity if observation is not None else None,
            attempted_act_identity,
            trial.attempt.pulse.scan_before,
            boundary,
        )
    )

    recorded_candidate = (
        _theory_attempt_identity(theory.theory_id, observation)
        if observation is not None
        and observation.disposition is TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
        and observation.source == source
        else None
    )
    recorded_attempt = (
        state.theory_state.ledger.attempts.get(recorded_candidate)
        if recorded_candidate is not None
        else None
    )
    recorded_id = (
        recorded_candidate
        if recorded_attempt is not None and recorded_attempt.execution_source is not None
        else None
    )
    attempt_id = recorded_id or (
        "execution-horizon-attempt",
        theory.theory_id,
        theory.current_version_id,
        source,
        advance_evidence,
    )
    selected_pairs = tuple(trial.attempt.bearing.act.policy.applied)
    if receipt is None or attempted_act_identity != receipt.selected_act:
        selected_pairs = ()
    if state.theory_state.ledger.attempts.get(attempt_id) is None:
        occurrence = advance_evidence
        _record_controlling_theory_fact(
            state,
            RecordTheoryAttempt(
                TheoryAttemptReceipt(
                    theory_id=theory.theory_id,
                    version_id=theory.current_version_id,
                    attempt_id=attempt_id,
                    source=source,
                    execution_ref=trial.execution.epoch_ref,
                    occurrence_evidence=occurrence,
                    act_identity=attempted_act_identity,
                    act_pairs=selected_pairs,
                    selected_act_pairs=tuple(trial.attempt.bearing.act.policy.action_pairs),
                    pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
                    disposition=TheoryAttemptDisposition.ACCEPTED_PROVISIONAL,
                    evidence=(advance_evidence,),
                    consumer_boundary=consumer_boundary,
                    execution_source=source,
                    observation_boundary=source,
                    program_transaction=ProgramTransaction.from_heading(
                        trial.attempt.bearing.act.policy.heading,
                        trial.execution.before_snap,
                    ),
                ),
            ),
        )
    accepted_attempt = state.theory_state.ledger.attempts[attempt_id]
    execution_source = accepted_attempt.execution_source or source
    selected_producer = bool(receipt is not None and receipt.kind == "selected-producer")
    starts_transaction = bool(
        selected_producer
        and selected_pairs
        and (current_scope is None or current_scope.transaction_attempt_id is None)
    )
    observes_consumer = bool(selected_producer and selected_pairs and consumer_boundary is not None)
    replays_owned_consumer = bool(
        current_scope is not None
        and current_scope.transaction_attempt_id is not None
        and current_scope.consumer_boundary is not None
        and policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
        and trial.execution.consumer_boundary_reached is True
    )
    extends_consumer_horizon = bool(observes_consumer or replays_owned_consumer)
    _record_controlling_theory_fact(
        state,
        AdvanceTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            accepted_attempt_id=attempt_id,
            source=source,
            boundary=boundary,
            advance_identity=("execution-horizon-advance", attempt_id, boundary),
            phase_receipts=(
                *(
                    (
                        TheoryPhaseReceipt(
                            kind=TheoryPhaseKind.SCAN_PROGRESS,
                            evidence_identity=(_semantic_key(receipt),),
                        ),
                    )
                    if receipt is not None
                    else ()
                ),
                *(
                    (
                        TheoryPhaseReceipt(
                            kind=TheoryPhaseKind.TRANSACTION_ATTEMPT,
                            evidence_identity=attempt_id,
                            execution_source=execution_source,
                        ),
                    )
                    if starts_transaction
                    else ()
                ),
                *(
                    (
                        TheoryPhaseReceipt(
                            kind=TheoryPhaseKind.CONSUMER_BOUNDARY,
                            evidence_identity=attempt_id,
                        ),
                    )
                    if observes_consumer
                    else ()
                ),
                *(
                    (
                        TheoryPhaseReceipt(
                            kind=TheoryPhaseKind.CONSUMER_STOP,
                            evidence_identity=attempt_id,
                            execution_tip=boundary,
                        ),
                    )
                    if extends_consumer_horizon
                    else ()
                ),
            ),
            execution_source=execution_source,
            remaining_budget=min(
                progress.remaining_budget,
                state.remaining_search_scans(ctx.max_scans),
            ),
        ),
    )
    checkpoint = _CausalCheckpoint(
        key=boundary.world_key,
        world=state.snapshot_world(),
        objective=trial.attempt.bearing.objective,
        configured_inputs=ctx.configured_inputs,
    )
    if all(
        _theory_boundary_from_checkpoint(current) != boundary
        for current in state.temporal_checkpoints
    ):
        state.temporal_checkpoints.append(checkpoint)


def _advance_retained_productive_tip(
    state: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    observation: _TheoryTransitionEvidence | None,
    *,
    prior_requirement_identities: frozenset[tuple[Any, ...]],
) -> None:
    """Advance to a monitor-retained consumer tip before refining its hazard."""

    receipt = trial.execution.scan_progress
    if receipt is None or state.work.state.scan_id != receipt.productive_scan:
        return
    novel = tuple(
        requirement
        for requirement in state.active_requirements
        if requirement.identity not in prior_requirement_identities
    )
    if not novel or any(
        requirement.source_checkpoint.world.work.state.scan_id != receipt.productive_scan
        or requirement.deadline.scan_id <= receipt.productive_scan
        for requirement in novel
    ):
        return
    _record_theory_execution_advance(state, ctx, trial, observation)
