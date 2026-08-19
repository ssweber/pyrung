"""Retain, continue, or revert a committed trial world.

After a trial passes verification, this module compares target distance and
earned-work marks, updates checkpoints, and classifies program-owned departures.
Regression handling delegates one bounded causal/replay transaction to
``recovery_investigation`` and applies its returned events. A clean departure
may remain pending until later earned-work evidence promotes it or requires
rollback. The terminal channel-departure handler owns that event stream and
policy arm after trend monitoring detects the occurrence.

This is the owner of post-commit recovery policy, not trial execution or local
gate acceptance.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from typing import TYPE_CHECKING

import pyrung.core.analysis.pilot.recording as recording
from pyrung.core.analysis.pilot.correction_lifecycle import (
    _promote_probationary_corrections,
)
from pyrung.core.analysis.pilot.departure import (
    DepartureClassification,
    DepartureDisposition,
    classify_departure,
    observe_departure,
)
from pyrung.core.analysis.pilot.departure_state import (
    DepartureAction,
    DepartureDecision,
    _assess_pending_departure,
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
    fulfilled_expectation_observations,
)
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
)
from pyrung.core.analysis.pilot.recovery_investigation import _investigate_and_revert
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    PilotEvent,
    TargetReached,
    _AcceptedTrial,
    _IterationFrame,
    _PilotContext,
    _PilotState,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    pass


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
