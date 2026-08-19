"""Drive the durable WorkingTheory lifecycle from accepted execution evidence.

This module owns opening, refining, rebasing, advancing, proving, and abandoning
the technician's persistent case.  It may restore the exact World named by a
temporal request and compose desired scan-entry configuration, but it does not
interpret raw execution evidence, choose a Bearing, execute one, or run the
outer Pilot loop.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

from pyrung.core.analysis.pilot.attempt_interpretation import (
    AttemptInterpretationKind,
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
    ActSource,
    Bearing,
    BearingObjective,
    ComposeCorrection,
    IntrascanPulse,
    LocalProgressKind,
    OrientationResult,
    _ActionPair,
    act_identity,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _merged_pilot_rungs,
    _pilot_rung_execution_receipt,
)
from pyrung.core.analysis.pilot.requirement_admission import requirement_condition_holds
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
    _theory_requirement_snapshot,
    _TheoryTransitionEvidence,
)
from pyrung.core.analysis.pilot.theory_reducer import (
    AbandonTheory,
    AdvanceTheory,
    ComposeTheoryCorrection,
    OpenTheory,
    ProveTheory,
    RebaseTheoryWorld,
    RecordTheoryAttempt,
    RefineTheory,
    RetainedCorrectionReceipt,
    reduce_theory,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    _AcceptedTrial,
    _AttemptResult,
    _HoldLogEntry,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import (
    IntrascanTracebackFinding,
    ProgramTransaction,
    TemporalNeedRequest,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryPhaseKind,
    TheoryPhaseReceipt,
    TheoryRequirementSnapshot,
    TheoryTemporalIntent,
    TheoryTermination,
    active_theory,
    active_theory_configurations,
    active_theory_pilot_rung_identities,
    active_theory_superseded_pilot_rung_identities,
    assert_temporal_need_current,
    theory_source_is_retained,
    theory_view,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import EpochRef

logger = logging.getLogger(__name__)


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
    failed_receipts = tuple(failed for _requirement, failed in exact_pairs)
    if len({failed.act_identity for failed in failed_receipts}) != 1:
        return False
    if len({id(failed.expectation) for failed in failed_receipts}) != 1:
        return False
    if len({id(failed.local_bearing) for failed in failed_receipts}) != 1:
        return False
    if len({failed.checkpoint_ref for failed in failed_receipts}) != 1:
        return False
    failed = failed_receipts[0]
    rebase_checkpoints = {
        requirement.checkpoint_ref: requirement.source_checkpoint
        for requirement, _failed in exact_pairs
    }
    if len(rebase_checkpoints) != 1:
        return False
    checkpoint = next(iter(rebase_checkpoints.values()))
    source = _theory_boundary_from_checkpoint(checkpoint)
    interpretation = interpret_failed_requirements(
        exact_pairs=exact_pairs,
        assertion_scan=max(requirement.deadline.scan_id for requirement, _ in exact_pairs),
    )
    if not interpretation.opens_theory:
        return False
    transition = _TheoryTransitionEvidence(
        claim=_theory_claim(failed.expectation, failed.local_bearing.objective, source),
        source=source,
        execution_ref=failed.execution_ref,
        occurrence_evidence=tuple(_semantic_key(item.explanation) for item in failed_receipts),
        act_identity=failed.act_identity,
        act_pairs=tuple(failed.local_bearing.act.policy.applied),
        selected_act_pairs=tuple(failed.local_bearing.act.policy.action_pairs),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=TheoryAttemptDisposition.REJECTED_EXACT,
        evidence=(
            ("program-guard-rebases", tuple(item.navigation_identity for _, item in rebases)),
        ),
        requirements=tuple(
            _theory_requirement_snapshot(requirement) for requirement, _failed in exact_pairs
        ),
        interpretation=interpretation,
        conductivity_observations=tuple(item.observation for item in failed_receipts),
    )
    _record_theory_transition(
        state,
        transition,
        remaining_budget=remaining_budget,
        record_fact=_record_controlling_theory_fact,
    )
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
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            attempt_identity=attempt_identity,
            source=trigger_source,
            execution_ref=failed.execution_ref,
            occurrence_evidence=tuple(_semantic_key(item.explanation) for item in failed_receipts),
            act_identity=failed.act_identity,
            act_pairs=tuple(failed.local_bearing.act.policy.applied),
            selected_act_pairs=tuple(failed.local_bearing.act.policy.action_pairs),
            pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
            disposition=TheoryAttemptDisposition.REJECTED_EXACT,
            evidence=(
                (
                    "program-guard-rebases",
                    tuple(requirement.navigation_identity for requirement, _failed in exact_pairs),
                ),
            ),
            conductivity_observations=tuple(item.observation for item in failed_receipts),
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
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            attempt_identity=attempt_identity,
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
            observation_boundary=observation.observation_boundary,
            program_transaction=observation.program_transaction,
            configurations=observation.configurations,
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


def _resolved_temporal_requirements(
    state: _PilotState,
    request: TemporalNeedRequest,
) -> tuple[ActiveRequirement, ...]:
    """Resolve the exact live requirements belonging to this temporal edge.

    A RETRY_TOGETHER refinement names only the newly observed need, while its
    triggering act may already contain corrective assignments learned by an
    earlier version.  Reconstruct that transaction from every still-active
    requirement in the current theory version.  Exact status matching below
    keeps discharged historical receipts out of executable navigation.
    """

    snapshots = tuple(request.requirements)
    if request.intent in {
        TheoryTemporalIntent.RETRY_TOGETHER,
        TheoryTemporalIntent.RETRY_THROUGH_DEADLINE,
    }:
        view = theory_view(state.theory_state)
        if (
            view is None
            or view.theory_id != request.theory_id
            or view.version_id != request.version_id
        ):
            raise ValueError("temporal retry does not match the active theory version")
        snapshots = tuple(view.requirements)
    return _resolve_temporal_requirement_snapshots(state, snapshots)


def _resolve_temporal_requirement_snapshots(
    state: _PilotState,
    snapshots: tuple[TheoryRequirementSnapshot, ...],
) -> tuple[ActiveRequirement, ...]:
    """Resolve detached requirement identities to unique current live objects."""

    resolved: list[ActiveRequirement] = []
    for snapshot in snapshots:
        matches = tuple(
            requirement
            for requirement in state.active_requirements
            if requirement.status is RequirementStatus.ACTIVE
            and _theory_requirement_snapshot(requirement).semantic_identity
            == snapshot.semantic_identity
        )
        if len(matches) > 1:
            raise ValueError(
                f"temporal need requirement is ambiguous: {snapshot.semantic_identity!r}"
            )
        # Theory versions retain historical requirement receipts. Once an
        # exact scan discharges one, it remains evidence but is no longer an
        # executable condition for a later temporal phase.
        if matches:
            resolved.append(matches[0])
    if not resolved:
        raise ValueError("temporal need has no unresolved live requirements")
    return tuple(resolved)


def _restore_temporal_source(
    state: _PilotState,
    request: TemporalNeedRequest,
    checkpoint: _CausalCheckpoint,
) -> None:
    """Restore the exact executable source selected by occurrence evidence."""

    live = _theory_live_boundary(state)
    if live == request.source:
        return

    retained_rungs = _normalize_retained_theory_overlays(
        state,
        tuple(state.pilot_rungs),
    )
    state.load_world(checkpoint.world)
    superseded = active_theory_superseded_pilot_rung_identities(state.theory_state)
    if superseded:
        state.pilot_rungs = pvector(
            rung for rung in state.pilot_rungs if _rung_identity(rung) not in superseded
        )
    # The checkpoint supplies the earlier runner boundary, not an earlier
    # theory of what PILOT has learned. Correctives established after that
    # boundary remain executable facts and are re-evaluated against the
    # restored snapshot. Appending preserves the overlay's last-owner rule.
    state.pilot_rungs = _merged_pilot_rungs(retained_rungs, state.pilot_rungs)
    state.pending_departure = None


def _normalize_retained_theory_overlays(
    state: _PilotState,
    retained_rungs: tuple[PilotRung, ...],
) -> tuple[PilotRung, ...]:
    """Collapse speculative re-scopes onto one exact theory-owned hold.

    A provisionally accepted inner route can install the same value as an
    established temporal hold with a shorter guard lifetime.  When that route
    is rolled back to the theory's source, its speculative lifetime has no
    ownership.  Reuse the exact owned rung only when ownership and the rung
    receipt are both unique; ambiguous or unrelated residue remains intact so
    the WorkingTheory reducer rejects the later rebase.
    """

    owned_identities = active_theory_pilot_rung_identities(state.theory_state)
    if not owned_identities:
        return retained_rungs
    superseded_overlays = active_theory_superseded_pilot_rung_identities(state.theory_state)

    rung_by_identity: dict[tuple[Any, ...], PilotRung] = {}
    for entry in state.hold_log:
        for rung in entry.pilot_rungs:
            identity = _rung_identity(rung)
            if identity in owned_identities:
                rung_by_identity.setdefault(identity, rung)
    for rung in retained_rungs:
        identity = _rung_identity(rung)
        if identity in owned_identities:
            rung_by_identity.setdefault(identity, rung)

    normalized: list[PilotRung] = []
    seen: set[tuple[Any, ...]] = set()
    for rung in retained_rungs:
        identity = _rung_identity(rung)
        if identity in superseded_overlays:
            continue
        related = tuple(
            owned
            for owned_identity, owned in rung_by_identity.items()
            if owned_identity[:2] == identity[:2]
        )
        selected = related[0] if identity not in owned_identities and len(related) == 1 else rung
        selected_identity = _rung_identity(selected)
        if selected_identity not in seen:
            normalized.append(selected)
            seen.add(selected_identity)
    return tuple(normalized)


def _rebase_restored_theory_world(
    state: _PilotState,
    request: TemporalNeedRequest,
    checkpoint: _CausalCheckpoint,
) -> TheoryBoundaryIdentity | None:
    """Retain an already-owned overlay added to one restored physical boundary."""

    live = _theory_live_boundary(state)
    if live == request.source:
        return None
    source_key = request.source.world_key
    live_key = live.world_key
    if (
        live.scan_id != request.source.scan_id
        or live.execution_ref != request.source.execution_ref
        or live.occurrence_identity != request.source.occurrence_identity
        or len(source_key) != 2
        or len(live_key) != 2
        or source_key[0] != live_key[0]
    ):
        raise ValueError("restored temporal source changed its physical execution boundary")
    source_rungs = tuple(source_key[1])
    live_rungs = tuple(live_key[1])
    retained = tuple(rung for rung in live_rungs if rung not in source_rungs)
    superseded = tuple(rung for rung in source_rungs if rung not in live_rungs)
    retained_identities = set(retained)
    retained_correction_receipts = tuple(
        RetainedCorrectionReceipt(
            receipt_id=receipt.receipt_id,
            correction_identity=receipt.identity,
            pilot_rung_identities=receipt.identity,
            origin_world_key=receipt.origin_key,
            status=receipt.status.value,
        )
        for receipt in state.correction_receipts
        if receipt.status.effective
        and any(identity in retained_identities for identity in receipt.identity)
    )
    owned_superseded = active_theory_superseded_pilot_rung_identities(state.theory_state)
    if not set(superseded) <= owned_superseded:
        raise ValueError("restored temporal source lost an unowned overlay")
    if not retained and not superseded:
        raise ValueError("restored temporal source changed without overlay evidence")
    _record_controlling_theory_fact(
        state,
        RebaseTheoryWorld(
            theory_id=request.theory_id,
            version_id=request.version_id,
            source=request.source,
            rebased_source=live,
            retained_pilot_rung_identities=retained,
            superseded_pilot_rung_identities=superseded,
            rebase_identity=(
                "working-theory-restored-world-rebase",
                request.version_id,
                request.source,
                live,
                retained,
                superseded,
            ),
            retained_correction_receipts=retained_correction_receipts,
        ),
    )
    rebased_checkpoint = _CausalCheckpoint(
        key=live.world_key,
        world=state.snapshot_world(),
        objective=checkpoint.objective,
        configured_inputs=checkpoint.configured_inputs,
    )
    if all(
        _theory_boundary_from_checkpoint(current) != live for current in state.temporal_checkpoints
    ):
        state.temporal_checkpoints.append(rebased_checkpoint)
    return live


def _temporal_source_checkpoint(
    state: _PilotState,
    request: TemporalNeedRequest,
    requirements: tuple[ActiveRequirement, ...],
) -> _CausalCheckpoint:
    """Resolve the retained scan immediately before the next temporal edge."""

    # A detached request remains valid historical attribution after its phase
    # advances, but only the active version's current progress tip may select
    # an executable checkpoint for another steer.
    assert_temporal_need_current(state.theory_state, request)

    checkpoints = tuple(
        {
            checkpoint.owner.reference: checkpoint
            for checkpoint in (
                *(requirement.source_checkpoint for requirement in requirements),
                *(receipt.source_checkpoint for receipt in state.expectation_receipts),
                *(receipt.source_checkpoint for receipt in state.failed_effect_receipts),
                *state.temporal_checkpoints,
            )
        }.values()
    )
    matches = tuple(
        checkpoint
        for checkpoint in checkpoints
        if _theory_boundary_from_checkpoint(checkpoint) == request.source
    )
    if not matches:
        raise ValueError("temporal need has no retained executable source checkpoint")
    return matches[-1]


def _setup_request_for_result(
    request: TemporalNeedRequest | None,
    result: OrientationResult,
) -> TemporalNeedRequest | None:
    if request is None:
        return None
    if not isinstance(result, Bearing):
        return None
    policy = result.act.policy
    view = getattr(getattr(result, "orientation", None), "world", None)
    view = getattr(getattr(view, "context", None), "theory_view", None)
    configured_continuation = bool(
        policy.local_progress is LocalProgressKind.TEMPORAL_EDGE
        and (
            getattr(view, "pending_configuration_identities", frozenset())
            or getattr(view, "pending_overlay_identities", frozenset())
        )
    )
    if (
        policy.source is not ActSource.WIDENING
        or (not policy.action_pairs and not configured_continuation)
        or isinstance(result.act, IntrascanPulse)
    ):
        return None
    return request


@dataclass(frozen=True)
class _TheoryCorrectionCompositionReceipt:
    """Exact no-scan WorkingTheory change produced by one composition."""

    requirements: tuple[ActiveRequirement, ...]
    configuration: ScanEntryConfiguration
    superseded_configuration_identities: tuple[tuple[Any, ...], ...]
    research_finding_identity: tuple[Any, ...] | None


def _compose_theory_correction(
    state: _PilotState,
    request: TemporalNeedRequest,
    result: ComposeCorrection,
) -> _TheoryCorrectionCompositionReceipt:
    """Persist desired entry configuration without changing the physical World."""

    theory = active_theory(state.theory_state)
    if theory is None or theory.theory_id != request.theory_id:
        raise ValueError("correction composition lost its active working theory")
    if theory.current_version_id != request.version_id:
        raise ValueError("correction composition addresses a stale theory version")
    precomposition_view = theory_view(state.theory_state)
    precomposition_scope = (
        precomposition_view.investigation_scope if precomposition_view is not None else None
    )
    retained_transaction_source = (
        precomposition_scope.execution_source
        if request.intent is TheoryTemporalIntent.RETRY_THROUGH_DEADLINE
        and precomposition_scope is not None
        and precomposition_scope.frontier == request.source
        and precomposition_scope.execution_source != request.source
        and precomposition_scope.transaction_act_identity is not None
        and precomposition_scope.retry_act_identity == precomposition_scope.transaction_act_identity
        else None
    )
    live_boundary = _theory_live_boundary(state)
    if live_boundary != request.source:
        raise ValueError(
            "correction composition is not at its restored source: "
            f"live={live_boundary!r} requested={request.source!r}"
        )
    matched = tuple(
        requirement
        for requirement in result.requirements
        if requirement in state.active_requirements
        and requirement.status is RequirementStatus.ACTIVE
    )
    if len(matched) != len(result.requirements) or not matched:
        raise ValueError("correction composition lost its exact live requirements")
    matched_identities = tuple(
        (
            _theory_requirement_snapshot(requirement).semantic_identity
            if getattr(requirement.condition, "tag", None) is None
            else requirement.identity
        )
        for requirement in matched
    )
    configuration = result.configuration
    destinations = frozenset(tag for tag, _value in configuration.assignments)
    superseded = tuple(
        active
        for active in active_theory_configurations(state.theory_state)
        if active.identity != configuration.identity
        and any(tag in destinations for tag, _value in active.assignments)
    )
    superseded_identities = tuple(item.identity for item in superseded)
    composition_identity = (
        "working-theory-compose",
        request.theory_id,
        request.version_id,
        request.source,
        matched_identities,
        configuration.identity,
        superseded_identities,
        result.research_finding_identity,
    )
    composed_source = _theory_live_boundary(state)
    _record_controlling_theory_fact(
        state,
        ComposeTheoryCorrection(
            theory_id=request.theory_id,
            version_id=request.version_id,
            source=request.source,
            composed_source=composed_source,
            requirement_identities=matched_identities,
            configuration=configuration,
            composition_identity=composition_identity,
            superseded_configuration_identities=superseded_identities,
            research_finding_identity=result.research_finding_identity,
        ),
    )
    theory = active_theory(state.theory_state)
    assert theory is not None
    # Preserve the transaction root only while the current trigger is exactly
    # the failure at that transaction's consumer stop. An old transaction
    # may remain in theory history after unrelated productive work; it cannot
    # pull a new correction hundreds of scans back to its former source.
    retry_source = retained_transaction_source or composed_source
    _record_controlling_theory_fact(
        state,
        RefineTheory(
            theory_id=request.theory_id,
            parent_version_id=theory.current_version_id,
            source=request.source,
            refined_source=composed_source,
            requirements=request.requirements,
            refinement_identity=("working-theory-composition-continue", composition_identity),
            temporal_intent=request.intent,
            trigger_attempt_id=request.trigger_attempt_id,
            temporal_source=retry_source,
        ),
    )
    # Installing temporary logic is a hypothesis, not proof that its parent
    # requirement is discharged. The refinement versions the exact composed
    # World while retaining the trigger and requirement scope, so fresh Compass
    # can retry/research with this one additional corrective in place.
    return _TheoryCorrectionCompositionReceipt(
        requirements=matched,
        configuration=configuration,
        superseded_configuration_identities=superseded_identities,
        research_finding_identity=result.research_finding_identity,
    )


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
    effective = _pilot_rung_execution_receipt(
        state.pilot_rungs,
        dict(source_checkpoint.world.work.state.tags),
    ).effective
    executed_pending = tuple(
        _rung_identity(rung) for rung in effective if _rung_identity(rung) in pending
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
            theory_id=request.theory_id,
            version_id=request.version_id,
            attempt_identity=attempt_id,
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
            program_transaction=ProgramTransaction.from_heading(
                bearing.act.policy.heading,
                dict(source_checkpoint.world.work.state.tags),
            ),
            configurations=execution_receipt.applied_configurations,
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


def _complete_controlled_setup(
    state: _PilotState,
    ctx: _PilotContext,
    controlled: _ControlledSetupAttempt,
    *,
    successor_need: bool = False,
) -> None:
    """Advance and promote one accepted temporal phase, unless it found another need."""

    request = controlled.request
    current_view = theory_view(state.theory_state)
    current_scope = current_view.investigation_scope if current_view is not None else None
    prior_transaction_pairs = tuple(getattr(current_scope, "transaction_act_pairs", ()))
    continues_transaction = bool(
        controlled.phase == "transaction"
        and current_scope is not None
        and current_scope.transaction_attempt_id is not None
        and prior_transaction_pairs
        and all(
            any(
                tag == candidate_tag and _values_match(value, candidate_value)
                for candidate_tag, candidate_value in controlled.setup_pairs
            )
            for tag, value in prior_transaction_pairs
        )
    )
    starts_transaction = bool(controlled.phase == "transaction" and not continues_transaction)
    observes_consumer = bool(
        controlled.phase == "transaction" and controlled.consumer_boundary is not None
    )
    extends_consumer_horizon = bool(
        controlled.phase == "transaction"
        and (
            observes_consumer
            or (
                continues_transaction
                and current_scope is not None
                and current_scope.consumer_boundary is not None
                and controlled.consumer_boundary_reached is True
            )
        )
    )
    superseded_rungs: tuple[PilotRung, ...] = ()
    if controlled.phase == "transaction":
        owned = active_theory_pilot_rung_identities(state.theory_state)
        stable_pairs = tuple(
            (tag, value)
            for tag, value in controlled.setup_pairs
            if tag not in ctx.edge_tags and tag not in ctx.clear_only
        )
        superseded_rungs = tuple(
            rung
            for rung in state.pilot_rungs
            if _rung_identity(rung) in owned
            and any(
                rung.dest == tag and not _values_match(rung.value, value)
                for tag, value in stable_pairs
            )
        )
        if superseded_rungs:
            superseded_ids = {_rung_identity(rung) for rung in superseded_rungs}
            state.pilot_rungs = pvector(
                rung for rung in state.pilot_rungs if _rung_identity(rung) not in superseded_ids
            )
            state.hold_log.append(
                _HoldLogEntry(
                    scan=state.work.state.scan_id,
                    source="revocation",
                    pilot_rungs=superseded_rungs,
                )
            )
    boundary = _theory_live_boundary(state)
    theory = active_theory(state.theory_state)
    if theory is None:
        raise ValueError("accepted temporal phase lost its active theory")
    progress = state.theory_state.ledger.progress[theory.current_progress_id]
    if boundary != progress.provisional_tip:
        setup_rung_identities = tuple(
            _rung_identity(rung)
            for rung in state.pilot_rungs
            if any(
                rung.dest == tag and _values_match(rung.value, value)
                for tag, value in controlled.setup_pairs
            )
        )
        setup_rung_identities = tuple(
            dict.fromkeys(
                (
                    *setup_rung_identities,
                    *controlled.executed_pending_overlay_identities,
                )
            )
        )
        _record_controlling_theory_fact(
            state,
            AdvanceTheory(
                theory_id=request.theory_id,
                version_id=request.version_id,
                accepted_attempt_id=controlled.attempt_id,
                source=request.source,
                boundary=boundary,
                advance_identity=(
                    "working-theory-setup-accepted",
                    controlled.attempt_id,
                    boundary,
                ),
                phase_receipts=(
                    *(
                        (
                            TheoryPhaseReceipt(
                                kind=(
                                    TheoryPhaseKind.REARM
                                    if controlled.phase == "rearm"
                                    else TheoryPhaseKind.CORRECTION_INSTALL
                                    if controlled.phase == "correction"
                                    else TheoryPhaseKind.TEMPORAL_SETUP
                                ),
                                evidence_identity=controlled.attempt_id,
                                requirement_identities=(controlled.local_requirement_identities),
                                pilot_rung_identities=setup_rung_identities,
                                superseded_pilot_rung_identities=tuple(
                                    _rung_identity(rung) for rung in superseded_rungs
                                ),
                                configurations=controlled.configurations,
                            ),
                        )
                        if controlled.phase != "transaction"
                        else ()
                    ),
                    *(
                        (
                            TheoryPhaseReceipt(
                                kind=TheoryPhaseKind.TRANSACTION_ATTEMPT,
                                evidence_identity=controlled.attempt_id,
                                requirement_identities=(controlled.local_requirement_identities),
                                pilot_rung_identities=setup_rung_identities,
                                superseded_pilot_rung_identities=tuple(
                                    _rung_identity(rung) for rung in superseded_rungs
                                ),
                                configurations=controlled.configurations,
                                execution_source=controlled.execution_source,
                            ),
                        )
                        if starts_transaction
                        else ()
                    ),
                    *(
                        (
                            TheoryPhaseReceipt(
                                kind=TheoryPhaseKind.CONSUMER_BOUNDARY,
                                evidence_identity=controlled.attempt_id,
                            ),
                        )
                        if observes_consumer
                        else ()
                    ),
                    *(
                        (
                            TheoryPhaseReceipt(
                                kind=TheoryPhaseKind.CONSUMER_STOP,
                                evidence_identity=controlled.attempt_id,
                                pilot_rung_identities=setup_rung_identities,
                                configurations=controlled.configurations,
                                execution_tip=boundary,
                            ),
                        )
                        if extends_consumer_horizon
                        else ()
                    ),
                ),
                remaining_budget=min(
                    progress.remaining_budget,
                    state.remaining_search_scans(ctx.max_scans),
                ),
                execution_source=controlled.execution_source,
            ),
        )
        checkpoint = _CausalCheckpoint(
            key=boundary.world_key,
            world=state.snapshot_world(),
            objective=controlled.objective,
            configured_inputs=ctx.configured_inputs,
        )
        if all(
            _theory_boundary_from_checkpoint(current) != boundary
            for current in state.temporal_checkpoints
        ):
            state.temporal_checkpoints.append(checkpoint)
    matched = _resolved_temporal_requirements(state, request)
    if controlled.phase == "rearm":
        # Rearm establishes only the trigger's release edge. Even when every
        # corrective condition happens to hold in that scan, the rejected
        # transaction has not yet been retried and none of its requirements
        # may be discharged. The unchanged temporal request is reread from the
        # newly advanced tip and Compass chooses the assertion phase afresh.
        return
    local_identities = set(controlled.local_requirement_identities)
    locally_established = tuple(
        requirement
        for requirement in matched
        if _theory_requirement_snapshot(requirement).semantic_identity in local_identities
        and requirement_condition_holds(
            requirement.condition,
            dict(state.work.state.tags),
        )
        is True
    )
    horizon_established = tuple(
        requirement
        for requirement in matched
        if extends_consumer_horizon
        and requirement.deadline.scan_id <= boundary.scan_id
        and requirement.demanding_occurrence.scan_id <= boundary.scan_id
        and requirement_condition_holds(
            requirement.condition,
            dict(state.work.state.tags),
        )
        is True
    )
    reached = target_reached(
        dict(state.work.state.tags),
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    established_identities = {
        requirement.identity for requirement in (*locally_established, *horizon_established)
    }
    discharged = (
        matched
        if not successor_need and reached
        else tuple(
            requirement for requirement in matched if requirement.identity in established_identities
        )
    )
    for requirement in discharged:
        index = state.active_requirements.index(requirement)
        state.active_requirements[index] = replace(
            requirement,
            status=RequirementStatus.DISCHARGED,
        )
    observations = tuple(
        (
            "requirement-discharged",
            _theory_requirement_snapshot(requirement).semantic_identity,
            controlled.attempt_id,
        )
        for requirement in discharged
    )
    if not successor_need and reached:
        _record_controlling_theory_fact(
            state,
            ProveTheory(
                theory_id=request.theory_id,
                version_id=request.version_id,
                promoted_landing=boundary,
                proof_identity=("working-theory-setup-proved", controlled.attempt_id, boundary),
                fulfilled_obligations=controlled.occurrence_evidence,
                requirement_observations=observations,
                retained_pilot_rung_identities=controlled.pilot_rung_identities,
                accepted_attempt_id=controlled.attempt_id,
            ),
        )
        return
    if successor_need:
        return
    # The phase held, but the complete target transaction has not yet been
    # promoted.  Advance the temporal question to the exact live subset at the
    # new World boundary.  This is a fresh Compass read, not an executable
    # suffix: WorkingTheory retains the transaction while the completed leaf
    # no longer masquerades as current work.
    matched_identities = {
        _theory_requirement_snapshot(requirement).semantic_identity for requirement in matched
    }
    remaining = tuple(
        requirement
        for requirement in state.active_requirements
        if requirement.status is RequirementStatus.ACTIVE
        and _theory_requirement_snapshot(requirement).semantic_identity in matched_identities
    )
    theory = active_theory(state.theory_state)
    if theory is None:
        raise ValueError("accepted temporal phase lost its active theory")
    remaining_snapshots = tuple(
        _theory_requirement_snapshot(requirement) for requirement in remaining
    )
    continuation_source = (
        request.source
        if remaining_snapshots and request.intent is TheoryTemporalIntent.RETRY_TOGETHER
        else boundary
    )
    _record_controlling_theory_fact(
        state,
        RefineTheory(
            theory_id=request.theory_id,
            parent_version_id=theory.current_version_id,
            source=continuation_source,
            refined_source=boundary,
            requirements=remaining_snapshots,
            refinement_identity=(
                "working-theory-phase-continue"
                if remaining_snapshots
                else "working-theory-phase-yield",
                controlled.attempt_id,
                tuple(item.semantic_identity for item in remaining_snapshots),
            ),
            temporal_intent=request.intent if remaining_snapshots else None,
            trigger_attempt_id=(request.trigger_attempt_id if remaining_snapshots else None),
            temporal_source=continuation_source if remaining_snapshots else None,
        ),
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
    fulfilled = attempts[-1].occurrence_evidence
    _record_optional_theory_fact(
        state,
        ProveTheory(
            theory_id=theory.theory_id,
            version_id=theory.current_version_id,
            promoted_landing=boundary,
            proof_identity=("theory-proved", theory.theory_id, boundary),
            fulfilled_obligations=fulfilled,
            requirement_observations=(),
            retained_pilot_rung_identities=tuple(
                _rung_identity(rung) for rung in state.pilot_rungs
            ),
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
                theory_id=theory.theory_id,
                version_id=theory.current_version_id,
                attempt_identity=attempt_id,
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
                program_transaction=ProgramTransaction.from_heading(
                    trial.attempt.bearing.act.policy.heading,
                    trial.execution.before_snap,
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
