"""Investigate one accepted departure and restore its recovery origin.

This module owns the bounded causal investigation transaction after post-commit
progress policy requests recovery. It may derive Working Theory evidence,
retain exact delayed requirements, and rebuild the selected checkpoint, but it
does not install corrections, monitor trials, or decide departure policy.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.recording as recording
import pyrung.core.analysis.pilot.theory_recording as _theory_recording
from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
from pyrung.core.analysis.pilot.departure import (
    DepartureResult,
)
from pyrung.core.analysis.pilot.departure_state import (
    _bank_pending_landing,
    _checkpoint_index,
    _open_pending_departure,
)
from pyrung.core.analysis.pilot.effect_observation import (
    fulfilled_expectation_observations,
)
from pyrung.core.analysis.pilot.effects import (
    EffectObservation,
    exact_first_departure_write,
    exact_last_landing_write,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.execution import ExecutionPoint
from pyrung.core.analysis.pilot.incidents import BearingDeparture
from pyrung.core.analysis.pilot.investigation_replay import (
    CausalOccurrence,
    RegressionWitness,
    _deviation_bearing,
    build_deviation_incident,
    incident_regression_witness,
)
from pyrung.core.analysis.pilot.navigation_contracts import act_identity
from pyrung.core.analysis.pilot.overlay import (
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.recovery import (
    recovery_transaction_active,
)
from pyrung.core.analysis.pilot.regression_requirements import (
    _MAX_TENTATIVE_PROOF_SCANS,
    _delayed_requirement_from_regression,
    _exact_correction_requirement_from_regression,
    _exact_regression_corrections,
    _ordinary_correction_order,
)
from pyrung.core.analysis.pilot.requirements import (
    ActiveRequirement,
    ExpectationReceipt,
    expectation_occurrence_ownerships,
    resolve_expectation_receipt_consumer,
)
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    PilotEvent,
    _AcceptedTrial,
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.pilot.world import _CausalCheckpoint, _RecoveryOrigin
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC


@dataclass(frozen=True)
class _ConductivityDepartureLink:
    """One root-owned transaction and its later consumed flow boundary."""

    source: EffectObservation
    frontier: EffectObservation
    departure: BearingDeparture
    harmful_write: Any
    projection: Any
    harmful_execution: ExecutionPoint


def _is_consumer_owned_same_scan_handback(
    projection: Any,
    consumer: Any,
    harmful_write: Any,
) -> bool:
    """Whether the consumer's own output causally triggers a later reset.

    A fulfilled handoff may deliberately be returned to its idle value later
    in the same scan.  Treat that as completion of the selected transaction,
    not as a delayed regression, but only when exact captured read sources
    connect an output of the declared consumer to the later write.  Merely
    sharing a scan is insufficient: an independently enabled watchdog remains
    a real departure.
    """

    if harmful_write.scan_id != consumer.scan_id:
        return False

    consumer_outputs = tuple(
        write
        for write in projection.writes
        if write.run_order == consumer.run_order
        and write.rung_id == consumer.rung_id
        and consumer.ordinal < write.ordinal <= harmful_write.ordinal
    )
    if not consumer_outputs:
        return False
    if any(write is harmful_write for write in consumer_outputs):
        return True

    causal_sources = {id(write.occurrence) for write in consumer_outputs}
    for write in projection.writes:
        if not consumer.ordinal < write.ordinal <= harmful_write.ordinal:
            continue
        if id(write.occurrence) in causal_sources:
            continue
        enabling_reads = projection.enabling_read_closure_observed_by_write(write)
        if any(id(read.occurrence.source) in causal_sources for read in enabling_reads):
            if write is harmful_write:
                return True
            causal_sources.add(id(write.occurrence))
    return False


def _productive_tip_checkpoint(
    trial: _AcceptedTrial,
    state: _PilotState,
    ctx: _PilotContext,
    source_checkpoint: Any,
    *,
    departure_scan: int | None,
) -> _CausalCheckpoint | None:
    """Retain an exact consumed scan only when a later deadline follows it.

    This is an executable prefix of the already-adopted act, not a folded
    continuation or predicted state.  The runner forks its immutable lineage
    at the ScanProgressReceipt's productive scan and the replay journal is
    clipped at that same physical boundary.
    """

    progress = trial.execution.scan_progress
    pulse = trial.attempt.pulse
    if (
        progress is None
        or progress.kind != "selected-producer"
        or departure_scan is None
        or departure_scan <= progress.productive_scan
        or progress.productive_scan not in pulse.kernel_scan_ids
        or pulse.projection_at(progress.productive_scan) is None
        or not state.work.history.contains(progress.productive_scan)
    ):
        return None
    return _checkpoint_at_scan(
        state,
        ctx,
        trial.attempt.bearing.objective,
        progress.productive_scan,
        configured_inputs=getattr(source_checkpoint, "configured_inputs", frozenset()),
    )


def _checkpoint_at_scan(
    state: _PilotState,
    ctx: _PilotContext,
    objective: Any,
    scan_id: int,
    *,
    configured_inputs: frozenset[str] = frozenset(),
) -> _CausalCheckpoint | None:
    """Retain one already-executed physical prefix as a causal source."""

    if (
        scan_id < 0
        or state.key_config is None
        or not state.work.history.contains(scan_id)
    ):
        return None
    work = fork_with_pilot_rungs(
        state.work,
        state.pilot_rungs,
        scan_id=scan_id,
    )
    committed = []
    for act in state.committed_acts:
        steps = tuple(
            step if step.scan_after <= scan_id else replace(step, scan_after=scan_id)
            for step in act.steps
            if step.scan_before < scan_id
        )
        if steps:
            committed.append(replace(act, steps=steps))
    world = state.world.set(
        work=work,
        committed_acts=pvector(committed),
    )
    key = _pilot_world_key(
        dict(work.state.tags),
        state.key_config,
        state.pilot_rungs,
        state.active_requirements,
    )
    configured = frozenset(
        {
            *getattr(ctx, "configured_inputs", frozenset()),
            *configured_inputs,
        }
    )
    return _CausalCheckpoint(
        key=key,
        world=world,
        objective=objective,
        configured_inputs=configured,
    )


def _activate_regression_theory_requirement(
    state: _PilotState,
    ctx: _PilotContext,
    bearing: Any,
    requirement: ActiveRequirement,
    source: _CausalCheckpoint,
    *,
    from_trend: Any,
    evidence: tuple[Any, ...],
    investigation: dict[str, Any],
) -> tuple[PilotEvent, ...] | None:
    """Record one regression fact, restore its source, and return to Compass."""

    transition = _theory_recording._record_theory_from_regression_requirement(
        state,
        requirement,
        bearing,
        evidence=evidence,
        remaining_budget=state.remaining_search_scans(ctx.max_scans),
    )
    if transition is None:
        return None
    if not any(
        current.navigation_identity == requirement.navigation_identity
        for current in state.active_requirements
    ):
        state.active_requirements.append(requirement)
    incident_scan = state.work.state.scan_id
    state.load_world(source.world)
    if all(current.owner is not source.owner for current in state.temporal_checkpoints):
        state.temporal_checkpoints.append(source)
    state.checkpoints.clear()
    state.pending_departure = None
    return (
        PilotEvent(
            "requirement_activated",
            requirement.deadline.scan_id,
            {"requirement": requirement.diagnostic_snapshot()},
        ),
        PilotEvent(
            "trend_regression",
            state.work.state.scan_id,
            {
                "from_trend": from_trend,
                "to_trend": state.best_trend,
                "checkpoint_key": source.key,
                "regression_nogoods": frozenset(),
                "pilot_rungs": tuple(state.pilot_rungs),
                "channel_transitions": (),
                "investigation": {
                    **investigation,
                    "working_theory": True,
                    "requirement": requirement.diagnostic_snapshot(),
                    "retained_suffix": False,
                    "incident_scan": incident_scan,
                },
                "revoked_corrections": (),
                "revoked_pilot_rungs": (),
            },
        ),
    )


def _activate_delayed_regression_requirement(
    state: _PilotState,
    ctx: _PilotContext,
    trial: _AcceptedTrial,
    regression_witness: RegressionWitness | None,
    generic_incident: Any,
    exact_witnesses: tuple[Any, ...],
    selected_exact: Any,
    current_act_identity: tuple[Any, ...],
    *,
    from_trend: Any,
) -> tuple[PilotEvent, ...] | None:
    """Prefer an exact accepted-effect receipt over a corrective hypothesis."""

    bearing = trial.attempt.bearing
    recovery_checkpoint = (
        _productive_tip_checkpoint(
            trial,
            state,
            ctx,
            selected_exact[1].source_checkpoint,
            departure_scan=regression_witness.departure_scan,
        )
        if selected_exact is not None
        and selected_exact[1].act_identity == current_act_identity
        and regression_witness is not None
        else None
    )
    delayed = _delayed_requirement_from_regression(
        state,
        ctx,
        regression_witness,
        recovery_checkpoint=recovery_checkpoint,
    )
    if delayed is None and exact_witnesses:
        delayed = _delayed_requirement_from_regression(
            state,
            ctx,
            incident_regression_witness(state.work, generic_incident),
        )
    if delayed is None:
        return None
    recovery_source, requirement, observation, failed_receipt = delayed
    appeared = observation.appeared
    transition = (
        _theory_recording._record_theory_from_failed_requirements(
            state,
            ((requirement, failed_receipt),),
            assertion_scan=appeared.scan_id,
            evidence=(
                (
                    "delayed-regression",
                    failed_receipt.act_identity,
                    appeared.scan_id,
                    requirement.deadline.scan_id,
                    requirement.navigation_identity,
                ),
            ),
            remaining_budget=state.remaining_search_scans(ctx.max_scans),
        )
        if appeared is not None
        else None
    )
    if transition is None:
        return None
    if not any(
        current.identity == failed_receipt.identity for current in state.failed_effect_receipts
    ):
        state.failed_effect_receipts.append(failed_receipt)
    if not any(
        current.navigation_identity == requirement.navigation_identity
        for current in state.active_requirements
    ):
        state.active_requirements.append(requirement)
    incident_scan = state.work.state.scan_id
    state.load_world(recovery_source.world)
    if all(current.owner is not recovery_source.owner for current in state.temporal_checkpoints):
        state.temporal_checkpoints.append(recovery_source)
    state.checkpoints.clear()
    state.pending_departure = None
    policy = bearing.act.policy
    return (
        PilotEvent(
            "candidate_rejected",
            incident_scan,
            {
                "index": 0,
                "candidate": recording._candidate_payload(policy),
                "applied": policy.applied,
                "co_actions": tuple(
                    pair for pair in policy.applied if pair != policy.primary_action
                ),
                "gates": trial.gate_events,
                "effect_observations": (failed_receipt.observation,),
                "post_commit": True,
            },
        ),
        PilotEvent(
            "failed_effect_explained",
            incident_scan,
            {"receipt": failed_receipt.diagnostic_snapshot()},
        ),
        PilotEvent(
            "requirement_activated",
            requirement.deadline.scan_id,
            {"requirement": requirement.diagnostic_snapshot()},
        ),
        PilotEvent(
            "trend_regression",
            state.work.state.scan_id,
            {
                "from_trend": from_trend,
                "to_trend": state.best_trend,
                "checkpoint_key": recovery_source.key,
                "regression_nogoods": frozenset(),
                "pilot_rungs": tuple(state.pilot_rungs),
                "channel_transitions": (),
                "investigation": {
                    "delayed_expectation": True,
                    "working_theory": True,
                    "requirement": requirement.diagnostic_snapshot(),
                    "receipt": failed_receipt.diagnostic_snapshot(),
                    "retained_suffix": False,
                },
                "revoked_corrections": (),
                "revoked_pilot_rungs": (),
            },
        ),
    )


def _investigate_and_revert(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    origin: _RecoveryOrigin,
    retain_if_unresolved: DepartureResult | None = None,
    settled_if_unresolved: PLC | None = None,
) -> tuple[PilotEvent, ...]:
    """Build an incident from ``origin`` through the current world and recover.

    Exact and legacy-derived corrections enter the ordinary Working Theory
    lifecycle.  A legacy correction that cannot name one exact, adjustable
    obstruction fails closed and is never installed privately.

    A regression origin anchors at its checkpoint, while a terminal-let-run
    ejection may anchor at the coast start. The origin owns that distinction;
    recovery derives the end from the committed world it is about to revert.
    """
    attempt = trial.attempt
    bearing_owner = attempt.bearing
    policy = bearing_owner.act.policy
    execution = trial.execution
    channel_motion = execution.channel_motion
    verified = trial.verification
    if not isinstance(verified, AssessedMotion):
        raise ValueError("target acceptance cannot enter regression investigation")
    checkpoint_index = _checkpoint_index(state, origin.checkpoint_owner)
    checkpoint = state.checkpoints[checkpoint_index]
    cp_key, cp_world, cp_trend = checkpoint.key, checkpoint.world, checkpoint.trend
    cp_fork = cp_world.work
    end_scan = state.work.state.scan_id
    legacy_unrepresentable = False
    investigation_payload: dict[str, Any] = {}
    if policy.chase_regression_causes:
        # A watch tag that moved TO a value the target still needs (the
        # checkpoint frontier) is *progress*, not a departure — the coast exists
        # to move it (Heat_CurStep 0->1 en route to 3).  Chasing it spawns
        # corrective holds against the plan itself (lock the enabler of the
        # very advance we wanted).  Only anomalous motion enters the bearing.
        expectation = bearing_owner.expectation
        fulfilled = (
            fulfilled_expectation_observations(
                expectation,
                attempt.effect_observations,
            )
            if expectation is not None
            else ()
        )
        # A terminal coast usually declares no producer of its own. Its
        # departure can nevertheless displace the exact landing receipt that
        # established the current channel tenure. Join that earlier accepted
        # occurrence before building the incident; otherwise the monitor sees
        # the harmful writer but has no source occurrence to hand to ordinary
        # failed-effect derivation.
        retained_sources: list[EffectObservation] = []
        if channel_motion.channel_tag is not None:
            tenure_value = execution.before_snap.get(channel_motion.channel_tag)
            live_epochs = tuple(
                epoch for epoch, _owner in state.work._causal_lineage.seal_through(end_scan)
            )
            for ownership in expectation_occurrence_ownerships(state.expectation_receipts):
                qualifying = tuple(
                    support
                    for support in ownership.supports
                    if any(support.receipt.execution_epoch is epoch for epoch in live_epochs)
                    if (
                        obligation := support.receipt.expectation.obligations[
                            support.obligation_index
                        ]
                    ).tag
                    == channel_motion.channel_tag
                    and _values_match(obligation.value, tenure_value)
                )
                if not qualifying:
                    continue
                consumed = tuple(
                    (support, consumer)
                    for support in qualifying
                    if (
                        consumer := resolve_expectation_receipt_consumer(
                            support.receipt,
                            support.obligation_index,
                        )
                    )
                    is not None
                )
                selected = (
                    consumed[0]
                    if len(consumed) == 1
                    else (qualifying[0], None)
                    if len(qualifying) == 1
                    else None
                )
                if selected is None:
                    continue
                support, consumer = selected
                receipt = support.receipt
                obligation = receipt.expectation.obligations[support.obligation_index]
                projection = state.work._replay_rung_write_projection_at(support.producer.scan_id)
                if projection is None:
                    continue
                retained_sources.append(
                    EffectObservation(
                        obligation=obligation,
                        disposition="SURVIVED",
                        appeared=support.producer,
                        consumer_read=consumer,
                        displacement=None,
                        observed_reads=(),
                        detail="accepted occurrence group established this channel tenure",
                        execution_owner=receipt.execution_owner,
                        execution_projection=projection,
                    )
                )
        retained_source: EffectObservation | None = None
        if retained_sources:
            latest_scan = max(item.appeared.scan_id for item in retained_sources if item.appeared)
            latest = tuple(
                item
                for item in retained_sources
                if item.appeared is not None and item.appeared.scan_id == latest_scan
            )
            if len(latest) == 1:
                retained_source = latest[0]
        exact_delayed_links: list[_ConductivityDepartureLink] = []
        bridged_frontier: EffectObservation | None = None
        consumed_frontiers = tuple(
            observation
            for observation in fulfilled
            if observation.consumer_read is not None
            and retained_source is not None
            and observation.obligation.tag == retained_source.obligation.tag
            and observation.appeared is not None
            and retained_source.appeared is not None
            and (observation.appeared.scan_id, observation.appeared.ordinal)
            > (retained_source.appeared.scan_id, retained_source.appeared.ordinal)
        )
        if retained_source is not None and len(consumed_frontiers) == 1:
            frontier = consumed_frontiers[0]
            consumer = frontier.consumer_read
            assert consumer is not None
            projections = tuple(
                projection
                for scan_id in range(consumer.scan_id, end_scan + 1)
                if (projection := state.work._replay_rung_write_projection_at(scan_id)) is not None
            )
            # The consumed frontier may legitimately hand the channel back to
            # its route source before the eventual bad landing (41 -> 40 ->
            # 91 in the neutral route).  The transaction receipt owns that
            # route source; select the exact later writer which owns the
            # observed final landing instead of misclassifying the ordinary
            # hand-back as the departure.
            departure_write = exact_last_landing_write(
                projections,
                after=consumer,
                tag=frontier.obligation.tag,
                target_value=frontier.obligation.value,
                landing_value=execution.after_snap.get(frontier.obligation.tag),
            )
            if departure_write is not None:
                projection, write = departure_write
                harmful_execution = state.world.execution_at(write.scan_id)
                if harmful_execution is not None:
                    bridged_frontier = frontier
                    exact_delayed_links.append(
                        _ConductivityDepartureLink(
                            source=retained_source,
                            frontier=frontier,
                            departure=BearingDeparture(
                                retained_source.obligation.tag,
                                retained_source.obligation.value,
                                write.scan_id,
                            ),
                            harmful_write=write,
                            projection=projection,
                            harmful_execution=harmful_execution,
                        )
                    )
        if not exact_delayed_links and retained_source is not None:
            fulfilled = (*fulfilled, retained_source)
        for observation in fulfilled:
            consumer = observation.consumer_read
            appeared = observation.appeared
            if appeared is None:
                continue
            start_scan = consumer.scan_id if consumer is not None else appeared.scan_id
            landing_value = execution.after_snap.get(observation.obligation.tag)
            projections = tuple(
                projection
                for scan_id in range(start_scan, end_scan + 1)
                if (projection := state.work._replay_rung_write_projection_at(scan_id)) is not None
            )
            landing = (
                exact_last_landing_write(
                    projections,
                    after=consumer,
                    tag=observation.obligation.tag,
                    target_value=observation.obligation.value,
                    landing_value=landing_value,
                )
                if consumer is not None and observation is bridged_frontier
                else exact_first_departure_write(
                    projections,
                    after=consumer,
                    tag=observation.obligation.tag,
                    tenure_value=observation.obligation.value,
                )
                if consumer is not None
                else exact_last_landing_write(
                    projections,
                    after=appeared,
                    tag=observation.obligation.tag,
                    target_value=observation.obligation.value,
                    landing_value=landing_value,
                )
            )
            if landing is not None:
                projection, write = landing
                if (
                    consumer is not None
                    and observation is not bridged_frontier
                    and _is_consumer_owned_same_scan_handback(projection, consumer, write)
                ):
                    # The declared consumer already fulfilled the handoff and
                    # its exact output ancestry owns the later hand-back.
                    continue
                harmful_execution = state.world.execution_at(write.scan_id)
                if harmful_execution is None:
                    continue
                exact_delayed_links.append(
                    _ConductivityDepartureLink(
                        source=observation,
                        frontier=observation,
                        departure=BearingDeparture(
                            observation.obligation.tag,
                            observation.obligation.value,
                            write.scan_id,
                        ),
                        harmful_write=write,
                        projection=projection,
                        harmful_execution=harmful_execution,
                    )
                )
        exact_delayed_departures = [link.departure for link in exact_delayed_links]
        delayed_bearing = tuple(
            (departure.tag, departure.value) for departure in exact_delayed_departures
        )
        coarse_bearing = _deviation_bearing(
            execution,
            frame,
            state.watch_tags,
            bearing_owner.objective.frontier,
        )
        bearing = delayed_bearing or coarse_bearing
        # The incident's evidence is the recorded step timelines inside the
        # window — the trend recorder's pen marks — never a history re-diff.
        # Committed acts are world-side, so reverted operations are already gone
        # and every timeline remains attached to its exact physical step group.
        window_timeline = tuple(
            event
            for act in state.committed_acts
            for receipt in act.context.execution_receipts
            for event in receipt.timeline
            if origin.anchor_scan <= event.scan <= end_scan
            and any(step.scan_before < event.scan <= step.scan_after for step in act.steps)
        )
        incident = build_deviation_incident(
            anchor_scan=origin.anchor_scan,
            end_scan=end_scan,
            action=policy.applied,
            bearing=bearing,
            before_snap=origin.before_snap,
            after_snap=execution.after_snap,
            timeline=window_timeline,
            channel_tag=channel_motion.channel_tag,
        )
        generic_incident = (
            build_deviation_incident(
                anchor_scan=origin.anchor_scan,
                end_scan=end_scan,
                action=policy.applied,
                bearing=coarse_bearing,
                before_snap=origin.before_snap,
                after_snap=execution.after_snap,
                timeline=window_timeline,
                channel_tag=channel_motion.channel_tag,
            )
            if delayed_bearing
            else incident
        )
        if exact_delayed_departures:
            incident = replace(
                incident,
                departure_scan=min(
                    departure.scan
                    for departure in exact_delayed_departures
                    if departure.scan is not None
                ),
                departures=tuple(exact_delayed_departures),
                changed_tags=tuple(
                    sorted(
                        {
                            *incident.changed_tags,
                            *(departure.tag for departure in exact_delayed_departures),
                        }
                    )
                ),
            )

        # Join later causes on the adopted live lineage.  The disposable pulse
        # fork can contain equal history under distinct Epoch/query objects;
        # expectation receipts are intentionally bound to ``state.work``.
        exact_witnesses: list[
            tuple[RegressionWitness, ExpectationReceipt, _ConductivityDepartureLink]
        ] = []
        for link in exact_delayed_links:
            observation = link.source
            if observation.appeared is None:
                continue
            departure = link.departure
            harmful_write = link.harmful_write
            projection = link.projection
            source_matches = tuple(
                (support.receipt, support.obligation_index, support.producer)
                for ownership in expectation_occurrence_ownerships(state.expectation_receipts)
                if ownership.occurrence == occurrence_snapshot(observation.appeared)
                for support in ownership.supports
                if support.receipt.expectation.obligations[support.obligation_index]
                is observation.obligation
            )
            if len(source_matches) != 1 or departure.scan is None:
                continue
            receipt, _index, producer = source_matches[0]
            source_link = CausalOccurrence(
                rung=producer.rung_id,
                tag=producer.transition.tag_name,
                value=producer.transition.to_value,
                scan_id=producer.scan_id,
                occurrence_ordinal=producer.ordinal,
                exact_write=producer,
                execution_owner=receipt.execution_owner,
                execution_projection=state.work._replay_rung_write_projection_at(producer.scan_id),
            )
            harmful_link = CausalOccurrence(
                rung=harmful_write.rung_id,
                tag=harmful_write.transition.tag_name,
                value=harmful_write.transition.to_value,
                scan_id=harmful_write.scan_id,
                occurrence_ordinal=harmful_write.ordinal,
                exact_write=harmful_write,
                execution_owner=link.harmful_execution.owner,
                execution_projection=projection,
            )
            exact_witnesses.append(
                (
                    RegressionWitness(
                        channel_tag=departure.tag,
                        source=departure.value,
                        departed=harmful_write.transition.to_value,
                        landing=execution.after_snap.get(departure.tag),
                        departure_scan=departure.scan,
                        cause=(harmful_link,),
                        causal_spine=frozenset(
                            (
                                departure.tag,
                                *(
                                    read.occurrence.name
                                    for read in projection.enabling_read_closure_observed_by_write(
                                        harmful_write
                                    )
                                ),
                            )
                        ),
                        owner_snapshot=dict(projection.entry_tags),
                        receipt_links=(source_link,),
                    ),
                    receipt,
                    link,
                )
            )
        current_act_identity = act_identity(bearing_owner.act)
        direct_current = tuple(
            item
            for item in exact_witnesses
            for witness, receipt, link in (item,)
            if receipt.act_identity == current_act_identity and link.source is link.frontier
        )
        current_owned = tuple(
            item
            for item in exact_witnesses
            for _witness, receipt, _link in (item,)
            if receipt.act_identity == current_act_identity
        )
        selected_exact = (
            direct_current[0]
            if len(direct_current) == 1
            else current_owned[0]
            if len(current_owned) == 1
            else exact_witnesses[0]
            if len(exact_witnesses) == 1
            else None
        )
        regression_witness = (
            selected_exact[0]
            if selected_exact is not None
            else incident_regression_witness(state.work, incident)
        )
        delayed_events = _activate_delayed_regression_requirement(
            state,
            ctx,
            trial,
            regression_witness,
            generic_incident,
            tuple(exact_witnesses),
            selected_exact,
            current_act_identity,
            from_trend=verified.trend,
        )
        if delayed_events is not None:
            return delayed_events
        # The correction belongs to the exact occurrence that exposed it.
        # Earned-work coordinates provide a finite executable guard; the old
        # rollback checkpoint is only a source of history and must not widen
        # the rung's lifetime.
        correction_anchor = (
            regression_witness.owner_snapshot
            if regression_witness is not None and regression_witness.owner_snapshot is not None
            else incident.before_snap
        )
        correction_progress_mark = (
            state.earned_work.mark(dict(correction_anchor))
            if state.earned_work is not None and state.earned_work.components
            else ()
        )
        exact_corrections = _exact_regression_corrections(
            state,
            ctx,
            incident,
            regression_witness,
            progress_mark=correction_progress_mark,
        )
        exact_requirement = None
        just_in_time_source = None
        exact_evidence = None
        for candidate_evidence in _ordinary_correction_order(exact_corrections):
            # Start at the latest possible source and widen only across the
            # small scan pipeline needed to make the real PilotRung visible at
            # the exact consumer.  This is bounded executor validation, not a
            # replay of the surrounding operation.
            for lead_scans in range(1, _MAX_TENTATIVE_PROOF_SCANS + 1):
                candidate_source = _checkpoint_at_scan(
                    state,
                    ctx,
                    bearing_owner.objective,
                    candidate_evidence.obstruction.scan_id - lead_scans,
                )
                if candidate_source is None:
                    continue
                candidate_requirement = _exact_correction_requirement_from_regression(
                    state,
                    ctx,
                    incident,
                    candidate_evidence,
                    candidate_source,
                )
                if candidate_requirement is None:
                    continue
                exact_evidence = candidate_evidence
                exact_requirement = candidate_requirement
                just_in_time_source = candidate_source
                break
            if exact_requirement is not None:
                break
        if (
            exact_requirement is not None
            and just_in_time_source is not None
            and exact_requirement.obstruction_occurrence is not None
        ):
            _theory_recording._advance_theory_to_regression_prefix(
                state,
                trial,
                just_in_time_source,
                exact_requirement.obstruction_occurrence,
            )
        if (
            exact_requirement is not None
            and just_in_time_source is not None
            and exact_evidence is not None
        ):
            activated = _activate_regression_theory_requirement(
                state,
                ctx,
                bearing_owner,
                exact_requirement,
                just_in_time_source,
                from_trend=verified.trend,
                evidence=(
                    (
                        "exact-regression-corrective",
                        exact_requirement.navigation_identity,
                        exact_evidence.hypothesis_kind,
                    ),
                ),
                investigation={
                    "exact_regression": True,
                    "private_replay": False,
                    "bounded_proof": True,
                    "hypothesis_kind": exact_evidence.hypothesis_kind,
                },
            )
            if activated is not None:
                return activated
        # Some exact incidents begin after operation-owned temporary inputs
        # have already disappeared from the retained log.  In that case a
        # tiny historical fork cannot reproduce the consumer even though the
        # exact causal writer names a finite, adjustable correction.  Keep the
        # correction tentative: restore the ordinary regression source, let
        # Working Theory compose the PilotRung, and make the fresh executor
        # continuation prove or reject it.  This is the same user-visible
        # steer -> obstruction -> steer loop, without a private incident
        # replay or an unbounded static cone.
        if exact_corrections:
            ordinary_source = _CausalCheckpoint(
                key=cp_key,
                world=cp_world,
                objective=bearing_owner.objective,
                    configured_inputs=getattr(ctx, "configured_inputs", frozenset()),
            )
            for candidate_evidence in _ordinary_correction_order(exact_corrections):
                candidate_requirement = _exact_correction_requirement_from_regression(
                    state,
                    ctx,
                    incident,
                    candidate_evidence,
                    ordinary_source,
                    require_bounded_proof=False,
                )
                if candidate_requirement is None:
                    continue
                activated = _activate_regression_theory_requirement(
                    state,
                    ctx,
                    bearing_owner,
                    candidate_requirement,
                    ordinary_source,
                    from_trend=verified.trend,
                    evidence=(
                        (
                            "tentative-regression-corrective",
                            candidate_requirement.navigation_identity,
                            candidate_evidence.hypothesis_kind,
                        ),
                    ),
                    investigation={
                        "exact_regression": True,
                        "private_replay": False,
                        "bounded_proof": False,
                        "validation": "ordinary-working-theory",
                        "hypothesis_kind": candidate_evidence.hypothesis_kind,
                    },
                )
                if activated is not None:
                    return activated
        if recovery_transaction_active():
            # This landing was observed while the already-selected local
            # repair transaction was being proved.  It may use the existing
            # exact receipt matcher above, but it may not start the legacy
            # hypothesis composer recursively. Restore the transaction's
            # checkpoint and hand the changed causal shape back to the fresh
            # outer read.
            state.load_world(cp_world)
            state.best_trend = cp_trend
            state.pending_departure = None
            return (
                PilotEvent(
                    "trend_regression",
                    state.work.state.scan_id,
                    {
                        "from_trend": verified.trend,
                        "to_trend": cp_trend,
                        "checkpoint_key": cp_key,
                        "regression_nogoods": frozenset(),
                        "pilot_rungs": tuple(state.pilot_rungs),
                        "channel_transitions": (),
                        "investigation": {"local_repair_handoff": True},
                        "revoked_corrections": (),
                        "revoked_pilot_rungs": (),
                    },
                ),
            )
        investigation_payload = {
            "retained_sources": len(retained_sources),
            "exact_delayed_links": len(exact_delayed_links),
            "exact_witnesses": len(exact_witnesses),
            "legacy_replay": False,
            "working_theory_admission": "unrepresentable",
        }
        legacy_unrepresentable = True
    if retain_if_unresolved is not None and not legacy_unrepresentable:
        # The departure earned no target-relative credit, but investigation found no
        # executable correction that preserves the target frontier.  The
        # independently-proven continuation therefore receives the ordinary
        # bounded pending window. If one is already open, retain its
        # original rollback boundary, budget, and the actual first observed
        # landing. The observer's later quiescent fork is evidence, not
        # permission to skip the next recomputation point.
        assert channel_motion.channel_tag is not None
        retained = PilotEvent(
            "departure_investigated",
            state.work.state.scan_id,
            {
                "channel_tag": channel_motion.channel_tag,
                "from_value": execution.before_snap.get(channel_motion.channel_tag),
                "retained": True,
                "progress": retain_if_unresolved.observation.progress,
                "investigation": investigation_payload,
            },
        )
        if state.pending_departure is not None:
            _bank_pending_landing(trial, state)
            return (retained,)
        assert settled_if_unresolved is not None
        return (
            retained,
            *_open_pending_departure(
                retain_if_unresolved,
                settled_if_unresolved,
                trial,
                state,
                ctx,
            ),
        )

    # Legibility (recording only): the channel transition(s) this revert undoes.
    # A destructive move (``S_StateCurrent 6->8`` Aborting) and a program-intended
    # useful program-owned move (``6->11`` Held) both leave the bearing, but only the former
    # is a genuine error — printing the reverted channel edge separates them in
    # every transcript.  Read the channel value at the checkpoint (from) vs. the
    # regressed frame (to); a channel is any opaque-loop pipeline register.
    channel_transitions: tuple[tuple[str, Any, Any], ...] = recording._channel_transitions(
        ctx, trial, cp_fork, execution.after_snap
    )

    # Keep the failed action as a nogood in the exact world where it was tried.
    # ``cp_key`` owns the rollback destination and may precede clean intermediate
    # actions inside one channel tenure; ``frame.key`` owns the action source.
    # A replay-confirmed correction changes that source key, so the same action
    # remains naturally eligible in the corrected executable world.
    regression_nogoods = set(policy.regression_nogoods)
    observations = [
        ActionNogoodObservation(frame.key, ("pair", pair)) for pair in regression_nogoods
    ]
    if len(policy.applied) > 1:
        observations.append(ActionNogoodObservation(frame.key, act_identity(bearing_owner.act)))
    if observations:
        ctx.compass, _ = ctx.compass.apply(tuple(observations))
    # A regression inside pending motion returns to its local checkpoint
    # and keeps the bounded attempt open. Only an outer revert ends it.
    if state.pending_departure is not None:
        rollback_index = _checkpoint_index(state, state.pending_departure.rollback_owner)
        local_checkpoint = checkpoint_index > rollback_index
        if not local_checkpoint:
            state.pending_departure = None
    # Later checkpoints are target-progress receipts inside the departed
    # channel tenure. Once the incident requires a correction/revert, they no
    # longer describe an executable clean world; return to the tenure owner.
    del state.checkpoints[checkpoint_index + 1 :]
    state.load_world(cp_world)
    state.best_trend = cp_trend
    return (
        PilotEvent(
            "trend_regression",
            state.work.state.scan_id,
            {
                "from_trend": verified.trend,
                "to_trend": cp_trend,
                "checkpoint_key": cp_key,
                "regression_nogoods": frozenset(regression_nogoods),
                "pilot_rungs": tuple(state.pilot_rungs),
                "channel_transitions": channel_transitions,
                "investigation": investigation_payload,
                "revoked_corrections": (),
                "revoked_pilot_rungs": (),
            },
        ),
    )
