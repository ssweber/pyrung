"""Resolve and execute the next durable WorkingTheory temporal request.

This module restores and rebases the exact World named by a temporal request,
resolves retained requirements, composes desired executable correction, and
completes controlled setup. Accepted fact application lives in
``theory_recording.py``. This module does not interpret raw execution evidence,
choose a Bearing, execute one, or run the outer Pilot loop.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.theory_recording as _theory_recording
from pyrung.core.analysis.pilot.execution import (
    ScanEntryConfiguration,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    Bearing,
    ComposeCorrection,
    IntrascanPulse,
    LocalProgressKind,
    OrientationResult,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _merged_pilot_rungs,
)
from pyrung.core.analysis.pilot.requirement_admission import requirement_condition_holds
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    RequirementStatus,
)
from pyrung.core.analysis.pilot.theory_evidence import (
    _theory_boundary_from_checkpoint,
    _theory_live_boundary,
    _theory_requirement_snapshot,
)
from pyrung.core.analysis.pilot.theory_reducer import (
    AdvanceTheory,
    ComposeTheoryCorrection,
    ProveTheory,
    RebaseTheoryWorld,
    RefineTheory,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    _HoldLogEntry,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import (
    TemporalNeedRequest,
    TheoryBoundaryIdentity,
    TheoryPhaseKind,
    TheoryPhaseReceipt,
    TheoryRequirementSnapshot,
    TheoryTemporalIntent,
    active_theory,
    active_theory_configurations,
    active_theory_pilot_rung_identities,
    active_theory_superseded_pilot_rung_identities,
    assert_temporal_need_current,
    theory_view,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint
from pyrung.core.analysis.pilot.world_key import _rung_identity, _semantic_key
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


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
    owned_superseded = active_theory_superseded_pilot_rung_identities(state.theory_state)
    if not set(superseded) <= owned_superseded:
        raise ValueError("restored temporal source lost an unowned overlay")
    if not retained and not superseded:
        raise ValueError("restored temporal source changed without overlay evidence")
    _theory_recording._record_controlling_theory_fact(
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
        policy.local_progress
        in {
            LocalProgressKind.TEMPORAL_EDGE,
            LocalProgressKind.THEORY_CORRECTIVE,
        }
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
    configuration: ScanEntryConfiguration | None
    pilot_rungs: tuple[PilotRung, ...]
    superseded_configuration_identities: tuple[tuple[Any, ...], ...]
    superseded_pilot_rung_identities: tuple[tuple[Any, ...], ...]
    research_finding_identity: tuple[Any, ...] | None


def _compose_theory_correction(
    state: _PilotState,
    request: TemporalNeedRequest,
    result: ComposeCorrection,
) -> _TheoryCorrectionCompositionReceipt:
    """Persist one correction, then expose its executable form to fresh Compass."""

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
            if getattr(requirement, "corrective_pilot_rungs", ())
            or getattr(requirement.condition, "tag", None) is None
            else requirement.identity
        )
        for requirement in matched
    )
    configuration = result.configuration
    pilot_rungs = tuple(result.pilot_rungs)
    superseded_identities: tuple[tuple[Any, ...], ...] = ()
    superseded_rungs: tuple[PilotRung, ...] = ()
    if configuration is not None:
        destinations = frozenset(tag for tag, _value in configuration.assignments)
        superseded = tuple(
            active
            for active in active_theory_configurations(state.theory_state)
            if active.identity != configuration.identity
            and any(tag in destinations for tag, _value in active.assignments)
        )
        superseded_identities = tuple(item.identity for item in superseded)
        executable_identity: Any = configuration.identity
    else:
        owned = active_theory_pilot_rung_identities(state.theory_state)
        destinations = frozenset(rung.dest for rung in pilot_rungs)
        new_identities = frozenset(_rung_identity(rung) for rung in pilot_rungs)

        def supersedes(active: PilotRung) -> bool:
            for proposed in pilot_rungs:
                if proposed.dest != active.dest:
                    continue
                if proposed.operation is None or active.operation is None:
                    return True
                if _semantic_key(proposed.operation.until) == _semantic_key(active.operation.until):
                    return True
            return False

        superseded_rungs = tuple(
            rung
            for rung in state.pilot_rungs
            if _rung_identity(rung) in owned
            and rung.dest in destinations
            and _rung_identity(rung) not in new_identities
            and supersedes(rung)
        )
        executable_identity = tuple(sorted(new_identities, key=repr))
    superseded_rung_identities = tuple(_rung_identity(rung) for rung in superseded_rungs)
    composition_identity = (
        "working-theory-compose",
        request.theory_id,
        request.version_id,
        request.source,
        matched_identities,
        executable_identity,
        superseded_identities,
        superseded_rung_identities,
        result.research_finding_identity,
    )
    composed_source = _theory_live_boundary(state)
    _theory_recording._record_controlling_theory_fact(
        state,
        ComposeTheoryCorrection(
            theory_id=request.theory_id,
            version_id=request.version_id,
            source=request.source,
            composed_source=composed_source,
            requirement_identities=matched_identities,
            composition_identity=composition_identity,
            configuration=configuration,
            pilot_rung_identities=tuple(_rung_identity(rung) for rung in pilot_rungs),
            superseded_configuration_identities=superseded_identities,
            superseded_pilot_rung_identities=superseded_rung_identities,
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
    _theory_recording._record_controlling_theory_fact(
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
    if pilot_rungs:
        superseded_set = set(superseded_rung_identities)
        retained = pvector(
            rung for rung in state.pilot_rungs if _rung_identity(rung) not in superseded_set
        )
        state.pilot_rungs = _merged_pilot_rungs(pilot_rungs, retained)
        if superseded_rungs:
            state.hold_log.append(
                _HoldLogEntry(
                    scan=state.work.state.scan_id,
                    source="working-theory-supersession",
                    pilot_rungs=superseded_rungs,
                )
            )
        state.hold_log.append(
            _HoldLogEntry(
                scan=state.work.state.scan_id,
                source="working-theory-composition",
                pilot_rungs=pilot_rungs,
            )
        )
    # Installing temporary logic is a hypothesis, not proof that its parent
    # requirement is discharged. The refinement versions the exact composed
    # World while retaining the trigger and requirement scope, so fresh Compass
    # can retry/research with this one additional corrective in place.
    return _TheoryCorrectionCompositionReceipt(
        requirements=matched,
        configuration=configuration,
        pilot_rungs=pilot_rungs,
        superseded_configuration_identities=superseded_identities,
        superseded_pilot_rung_identities=superseded_rung_identities,
        research_finding_identity=result.research_finding_identity,
    )


def _complete_controlled_setup(
    state: _PilotState,
    ctx: _PilotContext,
    controlled: _theory_recording._ControlledSetupAttempt,
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
        _theory_recording._record_controlling_theory_fact(
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
    if not successor_need and reached:
        _theory_recording._record_controlling_theory_fact(
            state,
            ProveTheory(
                theory_id=request.theory_id,
                version_id=request.version_id,
                proof_identity=("working-theory-setup-proved", controlled.attempt_id, boundary),
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
    _theory_recording._record_controlling_theory_fact(
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
