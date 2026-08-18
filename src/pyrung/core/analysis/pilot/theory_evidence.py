"""Interpret exact Pilot receipts into immutable WorkingTheory evidence.

This module describes what an attempt or monitored landing means.  It does not
mutate TheoryState, restore a World, compose a correction, or choose a Bearing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.attempt_interpretation import (
    AttemptInterpretation,
    AttemptInterpretationKind,
    interpret_attempt,
    interpret_failed_requirements,
)
from pyrung.core.analysis.pilot.effects import (
    ConsumerBoundary,
    EffectExpectation,
    EffectObservationSnapshot,
    displacement_consumer_read,
    effect_reached_consumer,
    occurrence_selector,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.execution import (
    ScanEntryConfiguration,
    execution_owner,
)
from pyrung.core.analysis.pilot.intrascan import IntrascanResult
from pyrung.core.analysis.pilot.navigation_contracts import (
    Bearing,
    BearingObjective,
    IntrascanPulse,
    ProgramScan,
    act_identity,
)
from pyrung.core.analysis.pilot.program_step import _program_step_from_bearing
from pyrung.core.analysis.pilot.requirement_evidence import (
    _attempt_productive_scan,
    _exact_failed_source,
    _selected_terminal_target_expectation,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    FailedEffectReceipt,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    _AcceptedTrial,
    _ActionPair,
    _AttemptResult,
    _BootstrapExecution,
    _CausalCheckpoint,
    _ExecutedAttempt,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.working_theory import (
    ProgramTransaction,
    TheoryAttemptDisposition,
    TheoryBoundaryIdentity,
    TheoryClaim,
    TheoryObjectiveSnapshot,
    TheoryObligationSnapshot,
    TheoryRequirementSnapshot,
    active_theory,
    theory_view,
)
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _rung_identity,
    _semantic_key,
)

if TYPE_CHECKING:
    from pyrung.core.runner import EpochRef


@dataclass(frozen=True)
class _TheoryTransitionEvidence:
    """Detached factual evidence returned before lifecycle reduction."""

    claim: TheoryClaim
    source: TheoryBoundaryIdentity
    execution_ref: EpochRef
    occurrence_evidence: tuple[Any, ...]
    act_identity: tuple[Any, ...]
    act_pairs: tuple[_ActionPair, ...]
    selected_act_pairs: tuple[_ActionPair, ...]
    pilot_rung_identities: tuple[tuple[Any, ...], ...]
    disposition: TheoryAttemptDisposition
    evidence: tuple[Any, ...]
    requirements: tuple[TheoryRequirementSnapshot, ...]
    interpretation: AttemptInterpretation
    program_transaction: ProgramTransaction | None = None
    conductivity_observations: tuple[EffectObservationSnapshot, ...] = ()
    consumer_boundary: ConsumerBoundary | None = None
    adopted_boundary: TheoryBoundaryIdentity | None = None
    investigation_frontier_id: tuple[Any, ...] | None = None
    producer_goal_id: tuple[Any, ...] | None = None
    observation_boundary: TheoryBoundaryIdentity | None = None
    configurations: tuple[ScanEntryConfiguration, ...] = ()

    @property
    def identity(self) -> tuple[Any, ...]:
        return (
            "observed-transition",
            self.claim.identity,
            self.source,
            self.execution_ref,
            self.occurrence_evidence,
            self.act_identity,
            self.pilot_rung_identities,
            self.disposition,
            self.evidence,
            self.interpretation,
            self.program_transaction,
            self.consumer_boundary,
            self.investigation_frontier_id,
            self.producer_goal_id,
            self.observation_boundary,
            tuple(configuration.identity for configuration in self.configurations),
        )

def _theory_occurrence_identity(occurrence: Any) -> tuple[Any, ...]:
    return (
        occurrence.kind,
        occurrence.tag,
        occurrence.scan_id,
        occurrence.dynamic_address,
        _semantic_key(occurrence.values),
        occurrence.enabled,
    )

def _theory_boundary_from_checkpoint(checkpoint: _CausalCheckpoint) -> TheoryBoundaryIdentity:
    if checkpoint.key is None:
        scan_id = checkpoint.world.work.state.scan_id
        return TheoryBoundaryIdentity(
            world_key=("unavailable-world-key", scan_id),
            scan_id=scan_id,
            owner_ref=checkpoint.owner.reference,
        )
    scan_id = checkpoint.world.work.state.scan_id
    owner = execution_owner(checkpoint.world.work, scan_id)
    # Requirement constraints belong to the theory version, not to the
    # physical scan-source identity. Compass world keys append them as a third
    # member for candidate/nogood isolation; strip that suffix here so adding a
    # later requirement does not manufacture a different historical source.
    raw_world_key = tuple(checkpoint.key)
    world_key = raw_world_key[:2] if len(raw_world_key) == 3 else raw_world_key
    if owner is None:
        # Boundary zero precedes the first execution epoch. Its retained
        # checkpoint owner is the exact source identity; the subsequent attempt
        # separately carries the owner of scan 1.
        return TheoryBoundaryIdentity(
            world_key=world_key,
            scan_id=scan_id,
            owner_ref=checkpoint.owner.reference,
        )
    execution_ref = owner.epoch.reference
    return TheoryBoundaryIdentity(
        world_key=world_key,
        scan_id=scan_id,
        owner_ref=execution_ref,
    )

def _theory_live_boundary(state: _PilotState) -> TheoryBoundaryIdentity:
    snapshot = dict(state.work.state.tags)
    key = (
        _pilot_world_key(
            snapshot,
            state.key_config,
            state.pilot_rungs,
            state.active_requirements,
        )
        if state.key_config is not None
        else ()
    )
    key = key[:2] if len(key) == 3 else key
    scan_id = state.work.state.scan_id
    owner = execution_owner(state.work, scan_id)
    if owner is None:
        raise ValueError("working theory requires one exact live execution owner")
    execution_ref = owner.epoch.reference
    world_key = tuple(key)
    return TheoryBoundaryIdentity(
        world_key=world_key,
        scan_id=scan_id,
        owner_ref=execution_ref,
    )

def _theory_objective_snapshot(objective: BearingObjective) -> TheoryObjectiveSnapshot:
    target = objective.target
    predicate = _semantic_key(target.predicate) if target.predicate is not None else None
    return TheoryObjectiveSnapshot(
        target_tag=target.tag,
        target_value=_semantic_key(target.value),
        predicate_identity=(predicate if isinstance(predicate, tuple) else (predicate,))
        if predicate is not None
        else None,
        frontier=tuple((tag, _semantic_key(value)) for tag, value in objective.frontier),
    )

def _theory_obligation_snapshot(obligation: Any) -> TheoryObligationSnapshot:
    selector = _semantic_key(getattr(obligation, "occurrence_selector", None))
    return TheoryObligationSnapshot(
        tag=obligation.tag,
        value=_semantic_key(obligation.value),
        producer=tuple(obligation.producer),
        consumer=tuple(obligation.consumer) if obligation.consumer is not None else None,
        required_shape=tuple(
            (tag, _semantic_key(value)) for tag, value in obligation.required_shape
        ),
        boundary=(
            (obligation.boundary[0], _semantic_key(obligation.boundary[1]))
            if obligation.boundary is not None
            else None
        ),
        terminal_target=obligation.terminal_target,
        polarity=str(getattr(obligation, "polarity", "produce")),
        occurrence_selector=(selector if isinstance(selector, tuple) else (selector,))
        if getattr(obligation, "occurrence_selector", None) is not None
        else None,
    )

def _theory_requirement_snapshot(requirement: ActiveRequirement) -> TheoryRequirementSnapshot:
    diagnostic = requirement.diagnostic_snapshot()
    condition = _semantic_key(diagnostic.condition)
    selected_writer = _semantic_key(diagnostic.selected_writer)
    scope = _semantic_key(diagnostic.scope)
    condition_identity = condition if isinstance(condition, tuple) else (condition,)
    selected_writer_identity = (
        selected_writer if isinstance(selected_writer, tuple) else (selected_writer,)
    )
    scope_identity = scope if isinstance(scope, tuple) else (scope,)
    obstruction_identity = (
        _theory_occurrence_identity(diagnostic.obstruction_occurrence)
        if diagnostic.obstruction_occurrence is not None
        else None
    )
    raw_source_world_key = diagnostic.source_world_key
    if isinstance(raw_source_world_key, tuple) and len(raw_source_world_key) == 3:
        # Requirement constraints version the current Compass world, not the
        # retained physical checkpoint. TheoryBoundaryIdentity applies the
        # same normalization so backward rebases compare like with like.
        raw_source_world_key = raw_source_world_key[:2]
    source_world_key = _semantic_key(raw_source_world_key)
    source_world_identity = (
        source_world_key if isinstance(source_world_key, tuple) else (source_world_key,)
    )
    semantic_identity = (
        "requirement",
        condition_identity,
        _theory_occurrence_identity(diagnostic.demanding_occurrence),
        _theory_occurrence_identity(diagnostic.deadline),
        selected_writer_identity,
        diagnostic.operand_authority.value,
        source_world_identity,
        diagnostic.causal_identity,
        diagnostic.phase.value,
        diagnostic.status.value,
        diagnostic.provenance,
        scope_identity,
        obstruction_identity,
    )
    return TheoryRequirementSnapshot(
        semantic_identity=semantic_identity,
        condition_identity=condition_identity,
        demanding_occurrence=_theory_occurrence_identity(diagnostic.demanding_occurrence),
        deadline_occurrence=_theory_occurrence_identity(diagnostic.deadline),
        selected_writer=selected_writer_identity,
        operand_authority=diagnostic.operand_authority.value,
        source_world_key=source_world_identity,
        source_scan=diagnostic.source_scan,
        execution_ref=requirement.execution_epoch.reference,
        phase=diagnostic.phase.value,
        status=diagnostic.status.value,
        provenance=diagnostic.provenance,
        scope=scope_identity,
        obstruction_occurrence=obstruction_identity,
    )

def _theory_claim(
    expectation: EffectExpectation,
    objective: BearingObjective,
    source: TheoryBoundaryIdentity,
) -> TheoryClaim:
    obligations = tuple(
        _theory_obligation_snapshot(obligation) for obligation in expectation.obligations
    )
    return TheoryClaim(
        source=source,
        objective=_theory_objective_snapshot(objective),
        obligations=obligations,
        selected_boundary=replace(
            source,
            occurrence_identity=(
                "selected-boundary",
                tuple(
                    (
                        item.producer,
                        item.consumer,
                        item.boundary,
                        item.polarity,
                        item.occurrence_selector,
                    )
                    for item in obligations
                ),
            ),
        ),
    )

def _theory_execution_evidence(
    execution: Any,
) -> tuple[
    Any,
    tuple[Any, ...],
    tuple[EffectObservationSnapshot, ...],
]:
    """Detach one exact attempt owner and its dynamic occurrence evidence."""

    observations = execution.effect_observations
    owner = execution_owner(execution.pulse.fork, execution.assertion_scan)
    if owner is None:
        raise ValueError("theory attempt requires one exact assertion owner")
    snapshots = tuple(observation.diagnostic_snapshot() for observation in observations)
    occurrence_evidence = tuple(_semantic_key(snapshot) for snapshot in snapshots)
    return (
        owner.epoch.reference,
        occurrence_evidence,
        snapshots,
    )

def _execution_consumer_boundary(execution: _ExecutedAttempt) -> ConsumerBoundary | None:
    """Pass one unambiguous consumed occurrence into its transaction receipt."""

    source_scan = execution.pulse.scan_before
    boundaries: list[ConsumerBoundary] = []
    for observation in execution.effect_observations:
        appeared = observation.appeared
        consumer = (
            observation.consumer_read
            or observation.displaced_read
            or displacement_consumer_read(observation)
        )
        if appeared is None or consumer is None:
            continue
        if observation.consumer_read is not None and not effect_reached_consumer(observation):
            continue
        if observation.consumer_read is None and observation.displacement is None:
            continue
        producer_projection = execution.projection_at(appeared.scan_id)
        consumer_projection = execution.projection_at(consumer.scan_id)
        if producer_projection is None or consumer_projection is None:
            continue
        producer = occurrence_selector(producer_projection, appeared)
        consumed = occurrence_selector(consumer_projection, consumer)
        if producer is None or consumed is None:
            continue
        try:
            boundary = ConsumerBoundary(
                produced_occurrence=occurrence_snapshot(appeared),
                consumer_occurrence=occurrence_snapshot(consumer),
                producer_selector=producer,
                consumer_selector=consumed,
                producer_scan_offset=appeared.scan_id - source_scan,
                consumer_scan_offset=consumer.scan_id - source_scan,
            )
        except ValueError:
            continue
        if boundary not in boundaries:
            boundaries.append(boundary)
    return boundaries[0] if len(boundaries) == 1 else None

def _merge_conductivity_observations(
    *groups: tuple[EffectObservationSnapshot, ...],
) -> tuple[EffectObservationSnapshot, ...]:
    """Combine immutable receipts without requiring their PLC values to be hashable."""

    merged: list[EffectObservationSnapshot] = []
    for group in groups:
        for observation in group:
            if observation not in merged:
                merged.append(observation)
    return tuple(merged)

def _program_transaction_receipt(
    bearing: Bearing,
    snapshot: dict[str, Any],
    observations: tuple[EffectObservationSnapshot, ...],
) -> ProgramTransaction | None:
    """Normalize declared or exactly observed program motion into one receipt."""

    declared = ProgramTransaction.from_heading(bearing.act.policy.heading, snapshot)
    if declared is not None:
        return declared
    target = bearing.objective.target
    for observation in observations:
        observed = ProgramTransaction.from_effect_observation(
            observation,
            channel_tag=target.tag,
            target_value=target.value,
        )
        if observed is not None:
            return observed
    return None

def _theory_transition_from_attempt(
    state: _PilotState,
    attempt: _AttemptResult,
    bearing: Bearing,
    checkpoint: _CausalCheckpoint | None,
    *,
    prior_requirement_identities: frozenset[tuple[Any, ...]],
    intrascan_report: IntrascanResult | None = None,
) -> _TheoryTransitionEvidence | None:
    """Detach theory evidence from the already-executed ordinary steer.

    The execution's effect observations already own their exact ordered
    projections. This adapter never forks, replays, or rebuilds a projection;
    unavailable shared evidence simply leaves the transition evidence absent.
    """

    execution = attempt.executed_attempt
    if execution is None or checkpoint is None:
        return None
    execution_receipt = execution.execution
    if execution_receipt is None:
        raise ValueError("theory transition has no immutable execution receipt")
    investigation_producer = (
        attempt.trial.execution.investigation_producer if attempt.trial is not None else None
    )
    intrascan_act = attempt.trial.execution.intrascan_act if attempt.trial is not None else None
    selection = bearing.investigation_selection
    if investigation_producer is not None and (
        selection is None
        or investigation_producer.frontier_id != selection.frontier_id
        or investigation_producer.producer_goal_id != selection.producer_goal_id
    ):
        raise ValueError("investigation producer receipt does not match its Bearing")
    if intrascan_act is not None and (
        not isinstance(bearing.act, (ProgramScan, IntrascanPulse))
        or intrascan_act.evidence_identity != bearing.act.evidence_identity
        or intrascan_act.expected_write_identity != _semantic_key(bearing.act.expected_write)
    ):
        raise ValueError("intrascan act receipt does not match its Bearing")
    selected_frontier_id = (
        investigation_producer.frontier_id
        if investigation_producer is not None
        else selection.frontier_id
        if selection is not None and attempt.trial is None
        else None
    )
    selected_goal_id = (
        investigation_producer.producer_goal_id
        if investigation_producer is not None
        else selection.producer_goal_id
        if selection is not None and attempt.trial is None
        else None
    )
    novel_requirements = tuple(
        requirement
        for requirement in state.active_requirements
        if requirement.identity not in prior_requirement_identities
    )
    route_lookahead_requirements = tuple(
        requirement
        for requirement in novel_requirements
        if requirement.provenance == "route-lookahead"
    )
    route_claim_expectation = None
    if route_lookahead_requirements and not execution.effect_observations:
        orientation = bearing.orientation
        if orientation is not None:
            route_claim_expectation = _selected_terminal_target_expectation(
                orientation.world.frame,
                bearing.objective.target,
                orientation.world.context,
            )
    intrascan_observations = (
        tuple(finding.observation.diagnostic_snapshot() for finding in intrascan_report.findings)
        if intrascan_report is not None
        else ()
    )
    if (
        not execution.effect_observations
        and not intrascan_observations
        and route_claim_expectation is None
        and investigation_producer is None
        and intrascan_act is None
    ):
        return None
    finding_obligations_list: list[Any] = []
    for finding in intrascan_report.findings if intrascan_report is not None else ():
        obligation = finding.observation.obligation
        if obligation not in finding_obligations_list:
            finding_obligations_list.append(obligation)
    finding_obligations = tuple(finding_obligations_list)
    immediate_expectation = execution.bearing.expectation
    findings_are_immediate = immediate_expectation is not None and all(
        any(obligation is current for current in immediate_expectation.obligations)
        for obligation in finding_obligations
    )
    claim_expectation = (
        immediate_expectation
        if finding_obligations and findings_are_immediate
        else EffectExpectation(finding_obligations)
        if finding_obligations
        else immediate_expectation or execution.landing_expectation or route_claim_expectation
    )
    source = _theory_boundary_from_checkpoint(checkpoint)
    execution_ref, effects, conductivity_observations = _theory_execution_evidence(execution)
    conductivity_observations = _merge_conductivity_observations(
        conductivity_observations,
        intrascan_observations,
    )
    if not effects and intrascan_observations:
        effects = tuple(_semantic_key(observation) for observation in intrascan_observations)
    if not effects and route_lookahead_requirements:
        effects = (
            (
                "route-lookahead",
                tuple(
                    _semantic_key(requirement.diagnostic_snapshot())
                    for requirement in route_lookahead_requirements
                ),
            ),
        )
    if investigation_producer is not None:
        effects = (
            *effects,
            ("investigation-producer", _semantic_key(investigation_producer)),
        )
    if intrascan_act is not None:
        effects = (
            *effects,
            ("intrascan-act", _semantic_key(intrascan_act)),
        )
    program_step = _program_step_from_bearing(bearing)
    productive_scan = _attempt_productive_scan(execution)
    interpretation = interpret_attempt(
        trial=attempt.trial,
        program_step=program_step,
        intrascan=intrascan_report,
        assertion_scan=productive_scan,
    )
    active = active_theory(state.theory_state)
    retained_claim = (
        state.theory_state.ledger.claims[active.claim_id]
        if (investigation_producer is not None or intrascan_act is not None) and active is not None
        else None
    )
    if claim_expectation is None and retained_claim is None:
        return None
    if retained_claim is not None:
        claim = retained_claim
    else:
        assert claim_expectation is not None
        claim = _theory_claim(claim_expectation, bearing.objective, source)
    requirements = tuple(
        _theory_requirement_snapshot(requirement) for requirement in novel_requirements
    )
    if route_lookahead_requirements and len(route_lookahead_requirements) == len(requirements):
        interpretation = AttemptInterpretation(
            AttemptInterpretationKind.SETUP_FIRST,
            "the retained look-ahead made a selected-route condition false",
            tuple(
                ("route-lookahead-requirement", requirement.navigation_identity)
                for requirement in route_lookahead_requirements
            ),
        )
    if requirements and not interpretation.opens_theory:
        selected_act = act_identity(bearing.act)
        exact_pairs = tuple(
            (requirement, failed)
            for requirement in state.active_requirements
            if requirement.identity not in prior_requirement_identities
            if (failed := _exact_failed_source(requirement, state)) is not None
            and failed.act_identity == selected_act
        )
        receipt_interpretation = interpret_failed_requirements(
            exact_pairs=exact_pairs,
            assertion_scan=productive_scan,
        )
        if receipt_interpretation.opens_theory:
            interpretation = receipt_interpretation
    return _TheoryTransitionEvidence(
        claim=claim,
        source=source,
        execution_ref=execution_ref,
        occurrence_evidence=effects,
        act_identity=act_identity(bearing.act),
        act_pairs=tuple(bearing.act.policy.applied),
        selected_act_pairs=tuple(bearing.act.policy.action_pairs),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=(
            TheoryAttemptDisposition.REJECTED_EXACT
            if requirements and interpretation.opens_theory
            else TheoryAttemptDisposition.ACCEPTED_PROVISIONAL
            if attempt.trial is not None
            else TheoryAttemptDisposition.REJECTED_EXACT
            if attempt.proof_rejection or requirements
            else TheoryAttemptDisposition.REJECTED_EMPIRICAL
        ),
        evidence=(
            ("effects", effects),
            ("gates", _semantic_key(attempt.gate_events)),
            ("investigation-producer", _semantic_key(investigation_producer)),
            ("intrascan-act", _semantic_key(intrascan_act)),
            (
                "interpretation",
                interpretation.kind.value,
                interpretation.reason,
                interpretation.supporting_identities,
            ),
        ),
        requirements=requirements,
        interpretation=interpretation,
        program_transaction=_program_transaction_receipt(
            bearing,
            dict(checkpoint.world.work.state.tags),
            conductivity_observations,
        ),
        conductivity_observations=conductivity_observations,
        consumer_boundary=_execution_consumer_boundary(execution),
        configurations=execution_receipt.applied_configurations,
        investigation_frontier_id=(selected_frontier_id),
        producer_goal_id=(selected_goal_id),
    )

def _theory_transition_after_monitor(
    state: _PilotState,
    observation: _TheoryTransitionEvidence | None,
    *,
    prior_requirement_identities: frozenset[tuple[Any, ...]],
    assertion_scan: int,
    trial: _AcceptedTrial | None = None,
    source_checkpoint: _CausalCheckpoint | None = None,
) -> tuple[_TheoryTransitionEvidence | None, frozenset[tuple[Any, ...]]]:
    """Fold normal post-commit receipts into the attempt's final interpretation."""

    novel = tuple(
        requirement
        for requirement in state.active_requirements
        if requirement.identity not in prior_requirement_identities
    )
    selected_act_identity = observation.act_identity if observation is not None else None
    exact_pairs = tuple(
        (requirement, failed)
        for requirement in novel
        if (failed := _exact_failed_source(requirement, state)) is not None
        and (
            selected_act_identity is None
            or failed.act_identity == selected_act_identity
            or (
                observation is not None
                and _failed_source_is_active_transaction(
                    state,
                    observation,
                    failed,
                )
            )
        )
    )
    progress = trial.execution.scan_progress if trial is not None else None
    landing_owns_tip = progress is None or progress.landing_owns_tip
    if not exact_pairs:
        return observation, frozenset()
    if observation is None:
        failed_receipts = tuple(failed for _requirement, failed in exact_pairs)
        checkpoints = {
            id(failed.source_checkpoint): failed.source_checkpoint for failed in failed_receipts
        }
        expectations = tuple(
            {id(failed.expectation): failed.expectation for failed in failed_receipts}.values()
        )
        act_identities = {failed.act_identity for failed in failed_receipts}
        execution_refs = {failed.execution_epoch.reference for failed in failed_receipts}
        bearings = {id(failed.local_bearing): failed.local_bearing for failed in failed_receipts}
        if (
            len(checkpoints) != 1
            or not expectations
            or len(act_identities) != 1
            or len(execution_refs) != 1
            or len(bearings) != 1
        ):
            return None, frozenset()
        checkpoint = next(iter(checkpoints.values()))
        obligations: list[Any] = []
        for expectation in expectations:
            for obligation in expectation.obligations:
                if obligation not in obligations:
                    obligations.append(obligation)
        if not obligations:
            return None, frozenset()
        expectation = EffectExpectation(tuple(obligations))
        bearing = next(iter(bearings.values()))
        selected_act_identity = next(iter(act_identities))
        source = _theory_boundary_from_checkpoint(source_checkpoint or checkpoint)
        execution_ref = next(iter(execution_refs))
        occurrence_evidence = tuple(_semantic_key(failed.explanation) for failed in failed_receipts)
        interpretation = interpret_failed_requirements(
            exact_pairs=exact_pairs,
            assertion_scan=(
                source_checkpoint.world.work.state.scan_id + 1
                if source_checkpoint is not None
                else assertion_scan
            ),
            landing_owns_tip=landing_owns_tip,
        )
        requirements = tuple(
            _theory_requirement_snapshot(requirement) for requirement, _failed in exact_pairs
        )
        if (
            source_checkpoint is not None
            and interpretation.opens_theory
            and all(current is not source_checkpoint for current in state.temporal_checkpoints)
        ):
            state.temporal_checkpoints.append(source_checkpoint)
        synthesized_observations = tuple(failed.observation for _requirement, failed in exact_pairs)
        synthesized = _TheoryTransitionEvidence(
            claim=_theory_claim(expectation, bearing.objective, source),
            source=source,
            execution_ref=execution_ref,
            occurrence_evidence=occurrence_evidence,
            act_identity=selected_act_identity,
            act_pairs=tuple(bearing.act.policy.applied),
            selected_act_pairs=tuple(bearing.act.policy.action_pairs),
            pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
            disposition=TheoryAttemptDisposition.REJECTED_EXACT,
            evidence=(("monitor-requirements", occurrence_evidence),),
            requirements=requirements,
            interpretation=interpretation,
            program_transaction=_program_transaction_receipt(
                bearing,
                dict((source_checkpoint or checkpoint).world.work.state.tags),
                synthesized_observations,
            ),
            conductivity_observations=synthesized_observations,
            investigation_frontier_id=(
                bearing.investigation_selection.frontier_id
                if bearing.investigation_selection is not None
                else None
            ),
            producer_goal_id=(
                bearing.investigation_selection.producer_goal_id
                if bearing.investigation_selection is not None
                else None
            ),
        )
        synthesized = _retain_investigation_transaction_source(state, synthesized)
        return synthesized, frozenset(requirement.identity for requirement, _ in exact_pairs)
    interpretation = interpret_failed_requirements(
        exact_pairs=exact_pairs,
        assertion_scan=assertion_scan,
        landing_owns_tip=landing_owns_tip,
    )
    evidence = tuple(
        item
        for item in observation.evidence
        if not (isinstance(item, tuple) and item and item[0] == "interpretation")
    ) + (
        (
            "interpretation",
            interpretation.kind.value,
            interpretation.reason,
            interpretation.supporting_identities,
        ),
    )
    requirements = tuple(
        _theory_requirement_snapshot(requirement) for requirement, _failed in exact_pairs
    )
    refined = replace(
        observation,
        source=_retained_later_deadline_source(state, observation, exact_pairs, interpretation),
        disposition=TheoryAttemptDisposition.REJECTED_EXACT,
        evidence=evidence,
        requirements=requirements,
        interpretation=interpretation,
        conductivity_observations=_merge_conductivity_observations(
            observation.conductivity_observations,
            tuple(failed.observation for _requirement, failed in exact_pairs),
        ),
    )
    refined = _retain_investigation_transaction_source(state, refined)
    return (
        refined,
        frozenset(requirement.identity for requirement, _failed in exact_pairs),
    )

def _frontier_within_consumer_stop(scope: Any) -> bool:
    """Whether the current frontier remains before its receipt-owned stop."""

    stop = getattr(scope, "consumer_stop", None)
    return bool(
        stop is not None
        and getattr(scope, "transaction_attempt_id", None) is not None
        and getattr(scope, "consumer_boundary_attempt_id", None) is not None
        and getattr(scope, "consumer_boundary", None) is not None
        and scope.frontier.scan_id <= stop.scan_id
    )

def _failed_source_is_active_transaction(
    state: _PilotState,
    observation: _TheoryTransitionEvidence,
    failed: FailedEffectReceipt,
) -> bool:
    """Correlate a later steer loss with its exact accepted transaction.

    A checkpoint steer can expose a delayed obstruction whose failed receipt
    belongs to the earlier reconnect act.  They are one investigation only
    when WorkingTheory names that act as the accepted transaction, the latest
    observation starts at its proved frontier, and the receipt's checkpoint
    is its exact execution root.
    """

    view = theory_view(state.theory_state)
    scope = view.investigation_scope if view is not None else None
    return bool(
        scope is not None
        and _frontier_within_consumer_stop(scope)
        and scope.transaction_act_identity is not None
        and observation.source == scope.frontier
        and failed.act_identity == scope.transaction_act_identity
        and _theory_boundary_from_checkpoint(failed.source_checkpoint) == scope.execution_source
    )

def _retained_later_deadline_source(
    state: _PilotState,
    observation: _TheoryTransitionEvidence,
    exact_pairs: tuple[tuple[ActiveRequirement, FailedEffectReceipt], ...],
    interpretation: AttemptInterpretation,
) -> TheoryBoundaryIdentity:
    """Use a proved productive tip only for an obstruction in a later scan."""

    if interpretation.kind is not AttemptInterpretationKind.RETRY_THROUGH_DEADLINE:
        return observation.source
    boundaries = tuple(
        {
            _theory_boundary_from_checkpoint(failed.source_checkpoint)
            for _requirement, failed in exact_pairs
        }
    )
    theory = active_theory(state.theory_state)
    progress = (
        state.theory_state.ledger.progress[theory.current_progress_id]
        if theory is not None
        else None
    )
    if (
        len(boundaries) == 1
        and progress is not None
        and boundaries[0] == progress.provisional_tip
        and boundaries[0].scan_id > observation.source.scan_id
    ):
        return boundaries[0]
    return observation.source

def _retain_investigation_transaction_source(
    state: _PilotState,
    observation: _TheoryTransitionEvidence,
) -> _TheoryTransitionEvidence:
    """Keep a same-transaction retry rooted at its exact accepted source.

    The obstruction remains attached to ``observation_boundary``. Only the
    executable source changes, and only when the current WorkingTheory view
    proves an accepted temporal transaction from that root to this frontier.
    """

    if observation.interpretation.kind is not AttemptInterpretationKind.RETRY_TOGETHER:
        return observation
    view = theory_view(state.theory_state)
    scope = view.investigation_scope if view is not None else None
    if (
        scope is None
        or not _frontier_within_consumer_stop(scope)
        or observation.source != scope.frontier
        or scope.execution_source == scope.frontier
        or scope.transaction_act_identity is None
    ):
        return observation
    return replace(
        observation,
        source=scope.execution_source,
        observation_boundary=scope.frontier,
        evidence=(
            *observation.evidence,
            (
                "investigation-scope",
                scope.execution_source,
                scope.frontier,
                scope.source_progress_id,
                scope.frontier_progress_id,
                scope.accepted_attempt_id,
            ),
        ),
    )

def _theory_bootstrap_transition(
    state: _PilotState,
    ctx: _PilotContext,
    receipt: _BootstrapExecution,
) -> _TheoryTransitionEvidence | None:
    """Detach the exact cold-start execution when it selected a factual claim."""

    if not receipt.appeared_effects:
        return None
    source = _theory_boundary_from_checkpoint(receipt.checkpoint)
    effects = tuple(
        _semantic_key(effect.diagnostic_snapshot()) for effect in receipt.appeared_effects
    )
    obligations = tuple(
        TheoryObligationSnapshot(
            tag=effect.designation.tag,
            value=_semantic_key(effect.designation.value),
            producer=tuple(effect.designation.producer),
            consumer=(
                tuple(effect.designation.consumer)
                if effect.designation.consumer is not None
                else None
            ),
            required_shape=tuple(
                (tag, _semantic_key(value)) for tag, value in effect.designation.required_shape
            ),
            boundary=(ctx.target.tag, _semantic_key(ctx.target.value)),
            terminal_target=effect.designation.tag == ctx.target.tag,
            polarity="produce",
            occurrence_selector=None,
        )
        for effect in receipt.appeared_effects
    )
    selected_boundary = replace(
        source,
        occurrence_identity=(
            "selected-bootstrap-boundary",
            receipt.scan_after,
            tuple((item.producer, item.consumer, item.boundary) for item in obligations),
        ),
    )
    claim = TheoryClaim(
        source=source,
        objective=_theory_objective_snapshot(receipt.checkpoint.objective),
        obligations=obligations,
        selected_boundary=selected_boundary,
    )
    requirements = tuple(
        _theory_requirement_snapshot(requirement)
        for requirement in state.active_requirements
        if requirement.source_checkpoint is receipt.checkpoint
    )
    reached = target_reached(
        dict(state.work.state.tags),
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    interpretation = AttemptInterpretation(
        (
            AttemptInterpretationKind.KEEP_AND_REREAD
            if reached and not requirements
            else AttemptInterpretationKind.SETUP_FIRST
            if requirements
            else AttemptInterpretationKind.UNRESOLVED
        ),
        (
            "the bootstrap landing reached the target"
            if reached and not requirements
            else "bootstrap evidence found a condition that must be established first"
            if requirements
            else "bootstrap evidence did not identify one actionable temporal requirement"
        ),
        (("bootstrap-effects", effects),),
    )
    return _TheoryTransitionEvidence(
        claim=claim,
        source=source,
        execution_ref=receipt.execution.epoch_ref,
        occurrence_evidence=("bootstrap-scan", receipt.scan_after, effects),
        act_identity=("executed-program-scan", receipt.scan_before, receipt.scan_after),
        act_pairs=(),
        selected_act_pairs=(),
        pilot_rung_identities=tuple(_rung_identity(rung) for rung in state.pilot_rungs),
        disposition=(
            TheoryAttemptDisposition.WITNESS
            if reached and not requirements
            else TheoryAttemptDisposition.REJECTED_EXACT
            if requirements
            else TheoryAttemptDisposition.INCOMPLETE
        ),
        evidence=(
            ("effects", effects),
            (
                "interpretation",
                interpretation.kind.value,
                interpretation.reason,
                interpretation.supporting_identities,
            ),
        ),
        requirements=requirements,
        interpretation=interpretation,
    )
