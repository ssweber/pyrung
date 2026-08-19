"""Retain, continue, or revert a committed trial world.

After a trial passes verification, this module compares target distance and
earned-work marks, updates checkpoints, and classifies program-owned departures.
Regression handling builds an incident, replay-validates corrective hypotheses,
installs at most one surviving correction, and restores the appropriate
checkpoint. A clean departure may remain pending until later earned-work evidence
promotes it or requires rollback. The terminal channel-departure handler owns
that event stream and policy arm after trend monitoring detects the occurrence.

This is the owner of post-commit recovery policy, not trial execution or local
gate acceptance.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.recording as recording
from pyrung.core.analysis.pilot.coast import coast_departure_tags
from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
from pyrung.core.analysis.pilot.correction_lifecycle import (
    _causally_harmful_corrections,
    _contradicted_corrections,
    _install_confirmed_correction,
    _promote_probationary_corrections,
    _revoke_corrections,
)
from pyrung.core.analysis.pilot.departure import (
    DepartureClassification,
    DepartureDisposition,
    DepartureResult,
    classify_departure,
    observe_departure,
)
from pyrung.core.analysis.pilot.departure_state import (
    DepartureAction,
    DepartureDecision,
    _assess_pending_departure,
    _bank_pending_landing,
    _bearing_satisfied,
    _channel_recovery_origin,
    _checkpoint_index,
    _checkpoint_recovery_origin,
    _departure_event_outcome,
    _open_pending_departure,
    _pending_departure_payload,
    _pending_recovery_index,
    _trial_checkpoint,
)
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWorkMovement,
    earned_work_is_useful_motion,
)
from pyrung.core.analysis.pilot.effects import (
    EffectObservation,
    exact_first_departure_write,
    exact_last_landing_write,
    fulfilled_expectation_observations,
    occurrence_snapshot,
)
from pyrung.core.analysis.pilot.execution import MotionKind
from pyrung.core.analysis.pilot.investigate import (
    CausalOccurrence,
    InvestigationRejection,
    InvestigationResult,
    RegressionWitness,
    ReplayIncident,
    _deviation_bearing,
    _replay_step,
    build_deviation_incident,
    build_replay_fn,
    incident_regression_witness,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.navigation_contracts import act_identity
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
)
from pyrung.core.analysis.pilot.overlay import (
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.recovery import (
    recovery_transaction_active,
)
from pyrung.core.analysis.pilot.regression_requirements import (
    _delayed_requirement_from_regression,
)
from pyrung.core.analysis.pilot.requirements import (
    ExpectationReceipt,
    expectation_occurrence_ownerships,
    resolve_expectation_receipt_consumer,
)
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    BearingDeparture,
    PilotEvent,
    TargetReached,
    _AcceptedTrial,
    _ActionPair,
    _CausalCheckpoint,
    _ConfirmedCorrection,
    _CorrectionReceipt,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _RecoveryOrigin,
)
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
    scan_id = progress.productive_scan
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
    assert state.key_config is not None
    key = _pilot_world_key(
        dict(work.state.tags),
        state.key_config,
        state.pilot_rungs,
        state.active_requirements,
    )
    configured = frozenset(
        {
            *ctx.configured_inputs,
            *getattr(source_checkpoint, "configured_inputs", frozenset()),
        }
    )
    return _CausalCheckpoint(
        key=key,
        world=world,
        objective=trial.attempt.bearing.objective,
        configured_inputs=configured,
    )


def _monitor_trend(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    verified = trial.verification
    attempt = trial.attempt
    policy = attempt.bearing.act.policy
    execution = trial.execution
    channel_ejection = execution.channel_motion.departed
    # A pending departure changes only the rollback boundary. Every trial inside
    # it still passes through ordinary trend, regression, investigation, and
    # candidate composition below. In particular, an
    # exact coast-departure receipt outranks the corridor's fallback expiry:
    # investigation owns that observed operation before pending lifetime
    # policy may discard it.
    if state.pending_departure is not None and not channel_ejection:
        applied = _apply_departure_decision(
            _assess_pending_departure(trial, state, ctx),
            trial,
            frame,
            state,
            ctx,
        )
        if applied is not None:
            yield from applied
            return

    if isinstance(verified, TargetReached):
        return

    assert state.best_trend is not None
    assessment = verified.assessment

    # An exposed bearing means the pilot knowingly exposed a world with
    # more prerequisites.  Commit the observation, but keep the previous
    # checkpoint and high-water mark alive: if the new world keeps drifting
    # away, the next verify pass should revert to the pre-frontier checkpoint
    # and chase the PLC-side cause.
    if assessment.bearing is BearingEffect.EXPOSED:
        yield PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": verified.trend,
                "key": verified.new_key,
                "checkpoint_count": len(state.checkpoints),
                "frontier": True,
                "baseline_trend": state.best_trend,
            },
        )
        return

    # A coast that *ejected* — the macro-state left the value it was held at
    # and wandered into a side branch (Execute -> Holding/Aborting). Route
    # Bearing and terminal let-run coasts share evidence and rollback mechanics.
    # That branch's trace distance is misleadingly LOW (fewer open leaves than the
    # held state), so the ordinary ``trend < best_trend`` test below would
    # checkpoint the ejection as progress.  It is not progress: the watchdog that
    # ejected fired *during the coast*, not after it.  Investigate over the
    # coast-span window (the fork's own history, ``scan_before -> fork end``) so
    # its exact channel-transition producer and upstream corrective levers are
    # recoverable, then revert to the pre-coast checkpoint.
    if channel_ejection:
        yield from _handle_channel_departure(trial, frame, state, ctx, verified)
        return

    # A satisfied channel bearing can enter a world whose backward
    # trace has a different coordinate system. Comparing its raw leaf count to
    # the source world is meaningless: Idle may be two leaves from Start,
    # while the expected Starting landing exposes fifteen production
    # prerequisites. Reset the trend baseline, but keep the source checkpoint
    # as the outer rollback receipt. The landing remains pending until ordinary
    # progress banks a checkpoint; if later motion ejects into Alarm,
    # investigation must replay the action itself so it can discover the
    # missing hold and return a corrected candidate to the outer loop.
    if _bearing_satisfied(trial) and verified.trend > state.best_trend:
        assert execution.channel_motion.channel_tag is not None
        channel_tag = execution.channel_motion.channel_tag
        previous = state.best_trend
        state.best_trend = verified.trend
        yield PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": verified.trend,
                "key": verified.new_key,
                "checkpoint_count": len(state.checkpoints),
                "channel": channel_tag,
                "channel_value": execution.after_snap.get(channel_tag),
                "baseline_trend": previous,
                "unbanked": True,
            },
        )
        return

    if verified.trend < state.best_trend:
        if state.pending_departure is not None:
            # Trace distance says this reading exposes fewer open leaves. That
            # is useful locally, but it is not a receipt for program work and
            # therefore cannot close an unresolved departure. Keep the local
            # checkpoint so piloting can continue; earned work alone decides whether
            # the departed corridor earned anything durable.
            state.checkpoints.append(_trial_checkpoint(trial, state))
            state.best_trend = verified.trend
            yield PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": verified.new_key,
                    "checkpoint_count": len(state.checkpoints),
                    "unbanked": True,
                },
            )
            return
        state.checkpoints.append(_trial_checkpoint(trial, state))
        state.best_trend = verified.trend
        promoted_corrections = _promote_probationary_corrections(state)
        checkpoint_event = PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": state.best_trend,
                "key": verified.new_key,
                "checkpoint_count": len(state.checkpoints),
                "promoted_corrections": promoted_corrections,
            },
        )
        yield checkpoint_event
        return

    if verified.trend == state.best_trend and assessment.bearing in {
        BearingEffect.SATISFIED,
        BearingEffect.UNCHANGED,
    }:
        state.checkpoints.append(_trial_checkpoint(trial, state))
        yield PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": state.best_trend,
                "key": verified.new_key,
                "checkpoint_count": len(state.checkpoints),
                "flat": True,
            },
        )
        return

    if verified.trend <= state.best_trend or not state.checkpoints:
        return

    origin = _checkpoint_recovery_origin(state, before_snap=frame.snap)
    if policy.chase_regression_causes:
        yield recording._investigation_started_event(trial, origin)
    yield from _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        origin=origin,
    )


def _handle_channel_departure(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    verified: AssessedMotion,
) -> Iterator[PilotEvent]:
    """Classify and resolve one observed channel departure.

    This is a terminal arm of trend monitoring: it streams the existing
    ejection and investigation events in order, then owns pending/open/retain
    policy for that occurrence.
    """
    attempt = trial.attempt
    pulse = attempt.pulse
    bearing = attempt.bearing
    policy = bearing.act.policy
    execution = trial.execution
    channel_motion = execution.channel_motion
    chan = channel_motion.channel_tag
    assert chan is not None
    departed_from = execution.before_snap.get(chan)
    investigated = bool(state.checkpoints)
    ejection = PilotEvent(
        "letrun_ejection",
        state.work.state.scan_id,
        {
            "channel_tag": chan,
            "from_value": departed_from,
            "requested_value": channel_motion.target_value,
            "to_value": execution.after_snap.get(chan),
            "observe_label": policy.observe_label,
            "coast_span": (pulse.scan_before, state.work.state.scan_id),
            "investigated": investigated,
            "reason": None if investigated else "no checkpoint to revert to",
        },
    )
    if not investigated:
        # No prior checkpoint to anchor the incident or revert to — the
        # ejected state stands committed. Surface why so the bail is visible
        # in the event stream rather than a silent ``return ()``.
        yield ejection
        return
    yield ejection
    yield PilotEvent(
        "departure_check_started",
        state.work.state.scan_id,
        {
            "channel_tag": chan,
            "from_value": departed_from,
            "to_value": execution.after_snap.get(chan),
        },
    )
    expectation = bearing.expectation
    fulfilled_observations = (
        fulfilled_expectation_observations(
            expectation,
            attempt.effect_observations,
        )
        if expectation is not None
        else ()
    )
    fulfilled_expectation = expectation is not None and len(fulfilled_observations) == len(
        expectation.obligations
    )
    # Classify BEFORE investigating (departure.py): program-owned motion may
    # preserve earned work and offer a clean forward route. Reverting
    # it would throw away the whole march, and investigation would honestly
    # confirm nothing. Affirmative clean-route evidence opens bounded pending
    # piloting; regression or unknown evidence follows the conservative
    # investigate-and-revert arm.
    exact_channel_sources = tuple(
        item.obligation.value for item in fulfilled_observations if item.obligation.tag == chan
    )
    classification_source = (
        exact_channel_sources[0] if len(exact_channel_sources) == 1 else departed_from
    )
    observation, settled_work = observe_departure(
        state,
        ctx,
        bearing.objective,
        chan,
        classification_source,
        execution.before_snap,
        occurrence_scan=next(
            (
                event.scan
                for event in execution.timeline
                if any(
                    tag == chan
                    and _values_match(before, classification_source)
                    and not _values_match(after, classification_source)
                    for tag, before, after in event.transitions
                )
            ),
            state.work.state.scan_id,
        ),
        landing_receipt=execution.coast_receipt,
        execution=execution,
    )
    departure = classify_departure(observation)
    if fulfilled_expectation and earned_work_is_useful_motion(trial.earned_work_receipt):
        departure = replace(
            departure,
            classification=DepartureClassification.CLEAN_CONTINUATION,
            reason="exact handoff accompanied target-relative continuation work",
        )
    if (
        fulfilled_expectation
        and departure.classification is not DepartureClassification.CLEAN_CONTINUATION
    ):
        # An exact handoff does not make every later departure a fault.  First
        # honor affirmative ProgramStep/earned-work evidence; only regressive
        # or unresolved motion enters delayed receipt matching.
        origin = _channel_recovery_origin(
            state,
            trial,
            frame,
            chan,
            departed_from,
        )
        if policy.chase_regression_causes:
            yield recording._investigation_started_event(trial, origin)
        yield from _investigate_and_revert(
            trial,
            frame,
            state,
            ctx,
            origin=origin,
            retain_if_unresolved=departure,
            settled_if_unresolved=settled_work,
            occurrence_requirements=observation.reading.external_supports,
        )
        return
    if departure.classification is DepartureClassification.CLEAN_CONTINUATION:
        prescribed_departure = (
            policy.route_prescribed and verified.assessment.agency is Agency.PILOT
        )
        if (
            observation.progress.movement is EarnedWorkMovement.UNCHANGED
            and not prescribed_departure
            and (
                state.pending_departure is not None
                or observation.reading.disposition is DepartureDisposition.REACTIVE
            )
        ):
            # A clean route says the landing is usable, but a known-preserved
            # progress receipt says this occurrence earned no program work.
            # For ambient motion it may therefore be a preventable ejection.
            #
            # Positive reactive attribution requires investigation on the
            # first occurrence. An already-open pending state retains the
            # established rule: every preserved departure is concrete new
            # evidence and must be understood before retention.
            origin = _channel_recovery_origin(
                state,
                trial,
                frame,
                chan,
                departed_from,
            )
            if policy.chase_regression_causes:
                yield recording._investigation_started_event(trial, origin)
            yield from _investigate_and_revert(
                trial,
                frame,
                state,
                ctx,
                origin=origin,
                retain_if_unresolved=departure,
                settled_if_unresolved=settled_work,
                occurrence_requirements=observation.reading.external_supports,
            )
            return
        if state.pending_departure is None:
            yield from _open_pending_departure(departure, settled_work, trial, state, ctx)
            return
        # A clean program-owned departure inside an existing bounded attempt
        # that earned work (or fulfilled an explicitly prescribed channel
        # transaction) is ordinary piloting. Keep the original rollback
        # boundary and budget; do not nest another pending departure.
        return
    origin = _channel_recovery_origin(
        state,
        trial,
        frame,
        chan,
        departed_from,
    )
    if policy.chase_regression_causes:
        yield recording._investigation_started_event(trial, origin)
    yield from _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        origin=origin,
        occurrence_requirements=(
            observation.reading.external_supports
            if observation.reading.disposition is DepartureDisposition.REACTIVE
            else ()
        ),
    )


def _apply_departure_decision(
    decision: DepartureDecision,
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[PilotEvent, ...] | None:
    """Apply one assessment to the exact receipts owned by pending state."""
    pending = state.pending_departure
    assert pending is not None
    if decision.action is DepartureAction.WAIT:
        return None

    rollback_index = _checkpoint_index(state, pending.rollback_owner)
    saved_progress = (
        state.checkpoints[_checkpoint_index(state, pending.saved_progress_owner)]
        if pending.saved_progress_owner is not None
        else None
    )
    state.pending_departure = None
    if decision.action is DepartureAction.PROMOTE:
        del state.checkpoints[rollback_index + 1 :]
        verified = trial.verification
        promoted_trend = verified.trend if isinstance(verified, AssessedMotion) else 0
        if isinstance(verified, AssessedMotion):
            state.checkpoints.append(_trial_checkpoint(trial, state, trend=promoted_trend))
        state.best_trend = promoted_trend
        promoted_corrections = _promote_probationary_corrections(state)
        return (
            PilotEvent(
                "pending_departure_promoted",
                state.work.state.scan_id,
                _pending_departure_payload(
                    pending,
                    after_source_mark_fields={
                        "landing_mark": (
                            state.earned_work.mark(trial.execution.after_snap)
                            if state.earned_work is not None
                            else ()
                        ),
                        "outcome": _departure_event_outcome(decision),
                        "trend": promoted_trend,
                        "checkpoint_count": len(state.checkpoints),
                        "terminal": isinstance(verified, TargetReached),
                        "promoted_corrections": promoted_corrections,
                    },
                ),
            ),
        )
    if decision.action is DepartureAction.REGRESS:
        event = PilotEvent(
            "pending_departure_regressed",
            state.work.state.scan_id,
            _pending_departure_payload(
                pending,
                before_source_mark_fields={"outcome": _departure_event_outcome(decision)},
            ),
        )
        regression = _investigate_and_revert(
            trial,
            frame,
            state,
            ctx,
            origin=_checkpoint_recovery_origin(
                state,
                checkpoint_index=_pending_recovery_index(state, pending),
            ),
        )
        return (event, *regression)

    assert decision.action is DepartureAction.EXPIRE
    del state.checkpoints[rollback_index + 1 :]
    if saved_progress is not None:
        state.checkpoints.append(saved_progress)
    checkpoint = state.checkpoints[-1]
    state.load_world(checkpoint.world)
    state.best_trend = checkpoint.trend
    return (
        PilotEvent(
            "pending_departure_expired",
            state.work.state.scan_id,
            _pending_departure_payload(
                pending,
                before_source_mark_fields={"outcome": _departure_event_outcome(decision)},
            ),
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
    occurrence_requirements: tuple[tuple[str, Any], ...] = (),
) -> tuple[PilotEvent, ...]:
    """Build a bounded incident from ``origin`` through the current world, replay-test
    corrective holds, install the confirmed ones, and revert to the selected
    checkpoint.

    A regression origin anchors at its checkpoint, while a terminal-let-run
    ejection may anchor at the coast start. The origin owns that distinction;
    recovery derives the end from the committed world it is about to revert.
    """
    attempt = trial.attempt
    pulse = attempt.pulse
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
    confirmed_correction: _ConfirmedCorrection | None = None
    reused_receipt: _CorrectionReceipt | None = None
    investigation: InvestigationResult | None = None
    revoked_receipts: tuple[_CorrectionReceipt, ...] = ()
    investigation_nogoods: set[_ActionPair] = set()
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
                        execution_epoch=receipt.execution_epoch,
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
            for event in act.context.execution.timeline
            if origin.anchor_scan <= event.scan <= end_scan
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

        # Replay re-arms each step's recorded session spec (kind + channel +
        # target) from the committed step context.
        replay_steps = tuple(
            _replay_step(step, act.context)
            for act in state.committed_acts
            for step in act.steps
            if step.scan_before >= cp_fork.state.scan_id
        )
        role_tags = coast_departure_tags(state, ctx)
        # Join later causes on the adopted live lineage.  The disposable pulse
        # fork can contain equal history under distinct Epoch/query objects;
        # expectation receipts are intentionally bound to ``state.work``.
        exact_witnesses: list[
            tuple[RegressionWitness, ExpectationReceipt, _ConductivityDepartureLink]
        ] = []
        sealed = state.work._causal_lineage.seal_through(end_scan)
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
            harmful_owner = next(
                (
                    (epoch, owner)
                    for epoch, owner in sealed
                    if epoch.first_scan <= harmful_write.scan_id <= epoch.last_scan
                ),
                None,
            )
            if len(source_matches) != 1 or harmful_owner is None or departure.scan is None:
                continue
            receipt, _index, producer = source_matches[0]
            epoch, owner = harmful_owner
            source_link = CausalOccurrence(
                rung=producer.rung_id,
                tag=producer.transition.tag_name,
                value=producer.transition.to_value,
                scan_id=producer.scan_id,
                occurrence_ordinal=producer.ordinal,
                exact_write=producer,
                execution_epoch=receipt.execution_epoch,
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
                execution_epoch=epoch,
                execution_owner=owner,
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
        delayed_requirement = _delayed_requirement_from_regression(
            state,
            ctx,
            regression_witness,
            recovery_checkpoint=recovery_checkpoint,
        )
        if delayed_requirement is None and exact_witnesses:
            regression_witness = incident_regression_witness(state.work, generic_incident)
            delayed_requirement = _delayed_requirement_from_regression(
                state,
                ctx,
                regression_witness,
            )
        if delayed_requirement is not None:
            recovery_source, requirement, _observation, failed_receipt = delayed_requirement
            # Same-scan failures restore the matched transaction source.  A
            # later exact deadline may instead retain its already-executed
            # productive tip.  In either case the selected checkpoint is an
            # executable prefix, never a folded future.
            incident_scan = state.work.state.scan_id
            state.load_world(recovery_source.world)
            if all(
                current.owner is not recovery_source.owner for current in state.temporal_checkpoints
            ):
                state.temporal_checkpoints.append(recovery_source)
            state.checkpoints.clear()
            state.pending_departure = None
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
                        "from_trend": verified.trend,
                        "to_trend": state.best_trend,
                        "checkpoint_key": recovery_source.key,
                        "regression_nogoods": frozenset(),
                        "pilot_rungs": tuple(state.pilot_rungs),
                        "channel_transitions": (),
                        "investigation": {
                            "delayed_expectation": True,
                            "requirement": requirement.diagnostic_snapshot(),
                            "receipt": failed_receipt.diagnostic_snapshot(),
                            "retained_suffix": False,
                        },
                        "revoked_corrections": (),
                        "revoked_pilot_rungs": (),
                    },
                ),
            )
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
        # A corrective fact belongs to the occurrence that exposed it. The
        # witness carries the scan-entry snapshot for the harmful writer; its
        # Earned-work coordinates distinguish a late fault from earlier useful work
        # inside the same coast. The rollback checkpoint is only where replay
        # starts and is not corrective context.
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
        regression_progress_floor = dict(cp_fork.state.tags)
        regression_progress_floor.update(correction_progress_mark)
        causally_revoked = _causally_harmful_corrections(
            state,
            regression_witness,
            incident.before_snap,
        )
        excluded_corrections = {
            *state.correction_nogoods.get(cp_key, ()),
            *(receipt.identity for receipt in causally_revoked),
        }
        replay = build_replay_fn(
            cp_fork,
            cp_trend,
            tuple(state.pilot_rungs),
            replay_steps,
            ctx=ctx,
            incident=ReplayIncident(
                channel_tag=channel_motion.channel_tag,
                channel_target=channel_motion.target_value,
                terminal_role_tags=(
                    role_tags if policy.motion is MotionKind.COAST_HOLDING_WORLD else None
                ),
                # The replay reproduces the incident, so its eject watch is the
                # departed channel alone when one exists (audit I2 — an explicit
                # caller decision, not buried dispatch); the full role set only
                # when no channel register is recognized.
                watch_roles=(
                    (channel_motion.channel_tag,)
                    if channel_motion.channel_tag is not None
                    else role_tags
                ),
                departure_bearing=tuple((d.tag, d.value) for d in incident.departures),
                regression_witness=regression_witness,
                earned_work=state.earned_work,
                progress_anchor=dict(cp_fork.state.tags),
                regression_progress_floor=(
                    regression_progress_floor if correction_progress_mark else None
                ),
            ),
        )

        # The register set the target still needs comes from the exact Bearing
        # that produced this incident. The live frame here is useless — a
        # terminal-let-run frame is a coast with no tree — and the rollback
        # checkpoint may predate this operation. Re-deriving from either loses
        # completion-frontier needs (``Sts_StateCurrent = 17``) that the target
        # tree alone cannot surface.
        needed = list(bearing_owner.objective.frontier)
        investigation = investigate_deviation(
            # Derive hypotheses from the PLC that actually observed the
            # incident.  Replay still starts from ``cp_fork`` above.
            pulse.fork,
            incident,
            ctx,
            replay,
            needed=needed,
            installed_pilot_rungs=tuple(state.pilot_rungs),
            correction_pilot_rungs=tuple(
                rung
                for receipt in state.correction_receipts
                if receipt.status.effective
                for rung in receipt.pilot_rungs
            ),
            correction_progress_mark=correction_progress_mark,
            occurrence_requirements=occurrence_requirements,
            excluded_corrections=frozenset(excluded_corrections),
            regression_witness=regression_witness,
        )
        investigation_nogoods.update(investigation.regression_nogoods)
        # Investigation has already derived a finite guard and replayed this
        # exact installed form. Post-commit recovery does not reinterpret that proof through
        # a second, globally-steady-hold rule.
        confirmed_correction = investigation.correction
        revoked_by_id = {
            receipt.receipt_id: receipt
            for receipt in (
                *causally_revoked,
                *_contradicted_corrections(state, investigation, incident.before_snap),
            )
        }
        revoked_receipts = tuple(revoked_by_id.values())
        revoked_receipt_ids = {receipt.receipt_id for receipt in revoked_receipts}
        reused_receipt = next(
            (
                receipt
                for receipt in state.correction_receipts
                if receipt.status.effective
                and receipt.receipt_id not in revoked_receipt_ids
                and confirmed_correction is not None
                and receipt.identity == confirmed_correction.identity
            ),
            None,
        )
        if reused_receipt is not None:
            # The skeleton owns one correction for one exact executable form.
            # A repeated incident may reconfirm that form before useful work
            # promotes it, but it must not report or install a second
            # correction.  The existing receipt remains the sole authority.
            confirmed_correction = None

        def _hyp_detail(h: Any) -> dict[str, Any]:
            return {
                "kind": h.kind,
                "holds": h.holds,
                "sources": h.sources,
                "detail": h.detail,
            }

        def _rejection_detail(rejection: InvestigationRejection) -> dict[str, Any]:
            return {
                **_hyp_detail(rejection.hypothesis),
                "slug": rejection.slug,
                "ground": rejection.ground,
            }

        investigation_payload = {
            "retained_sources": len(retained_sources),
            "exact_delayed_links": len(exact_delayed_links),
            "exact_witnesses": len(exact_witnesses),
            "hypotheses": len(investigation.hypotheses),
            "confirmed": 0 if reused_receipt is not None else len(investigation.confirmed),
            "rejected": len(investigation.rejected),
            "unresolved": investigation.unresolved,
            "hypothesis_detail": tuple(_hyp_detail(h) for h in investigation.hypotheses),
            "confirmed_detail": (
                ()
                if reused_receipt is not None
                else tuple(_hyp_detail(h) for h in investigation.confirmed)
            ),
            "rejected_detail": tuple(_rejection_detail(r) for r in investigation.rejected),
            "revoked_corrections": tuple(receipt.receipt_id for receipt in revoked_receipts),
            **(
                {"reused_correction": reused_receipt.receipt_id}
                if reused_receipt is not None
                else {}
            ),
        }
    if (
        retain_if_unresolved is not None
        and confirmed_correction is None
        and reused_receipt is None
        and not revoked_receipts
    ):
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
    regression_nogoods = set(investigation_nogoods)
    regression_nogoods.update(policy.regression_nogoods)
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
    revoked_ids = _revoke_corrections(state, revoked_receipts)
    if confirmed_correction is not None:
        # Revocation removes contradicted owners before this append.  The
        # replay-confirmed remedy is therefore an ownership replacement, not a
        # second hold layered over the stale correction.
        correction_origin_key = state.checkpoints[-1].key
        _install_confirmed_correction(
            state,
            confirmed_correction,
            origin_key=correction_origin_key,
            scan=cp_fork.state.scan_id,
            source="investigation",
        )
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
                "revoked_corrections": revoked_ids,
                "revoked_pilot_rungs": tuple(
                    rung for receipt in revoked_receipts for rung in receipt.pilot_rungs
                ),
            },
        ),
    )
