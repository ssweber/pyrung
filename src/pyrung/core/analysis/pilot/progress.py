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

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from pyrsistent import pvector

import pyrung.core.analysis.pilot.recording as recording
from pyrung.core.analysis.pilot.coast import coast_departure_tags
from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
from pyrung.core.analysis.pilot.departure import (
    DepartureClassification,
    DepartureDisposition,
    DepartureObservation,
    DepartureResult,
    classify_departure,
    observe_departure,
)
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWorkMovement,
    EarnedWorkReceipt,
)
from pyrung.core.analysis.pilot.investigate import (
    InvestigationRejection,
    InvestigationResult,
    RegressionWitness,
    ReplayIncident,
    _deviation_bearing,
    _replay_step,
    build_deviation_incident,
    build_replay_fn,
    correction_identity,
    incident_regression_witness,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.navigation_contracts import BearingObjective
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    ProgressEffect,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _append_pilot_rungs,
    _pilot_rung_execution_receipt,
    _set_pilot_rungs,
    fork_with_pilot_rungs,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    CorrectionStatus,
    MotionKind,
    PilotEvent,
    TargetReached,
    _AcceptedTrial,
    _ActionPair,
    _Checkpoint,
    _CheckpointOwner,
    _ConfirmedCorrection,
    _CorrectionReceipt,
    _HoldLogEntry,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _RecoveryOrigin,
)
from pyrung.core.analysis.pilot.world_key import (
    _pilot_world_key,
    _rung_identity,
    _semantic_key,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

_PENDING_DEPARTURE_SCAN_BUDGET = 2000


class DepartureAction(Enum):
    """What progress policy should do with an unresolved departure."""

    WAIT = "wait"
    PROMOTE = "promote"
    REGRESS = "regress"
    EXPIRE = "expire"


class DepartureBasis(Enum):
    """Exceptional policy evidence applied without rewriting earned-work facts."""

    PILOT_CAUSED_REGRESSION = "pilot_caused_regression"


@dataclass(frozen=True)
class DepartureDecision:
    """One evidence-based assessment of a pending departure."""

    action: DepartureAction
    receipt: EarnedWorkReceipt
    basis: DepartureBasis | None = None


@dataclass(frozen=True)
class PendingDeparture:
    """Progress policy for one durable departure observation.

    Carries departure's opening observation intact and adds only mutable policy
    anchors: the post-settlement progress mark, the stable rollback-checkpoint
    owner, an optional saved-progress owner, and a finite search-scan
    deadline.  The saved-progress owner is an irreversible recovery floor —
    expiry and regression may discard only work after it; until it exists, the
    opening rollback owner remains the floor.  Correction install/revoke may
    replace a checkpoint's executable artifact but must preserve that owner.
    Policy resolves in two phases: a plain :class:`DepartureDecision` (wait,
    promote, regress, or expire) is computed first, then applied to the
    receipts this record owns.
    """

    opening: DepartureObservation
    earned_work_mark: tuple[tuple[str, Any], ...]
    rollback_owner: _CheckpointOwner
    expires_at_search_scan: int
    saved_progress_owner: _CheckpointOwner | None = None


def _checkpoint_index(state: _PilotState, owner: _CheckpointOwner) -> int:
    """Locate an exact checkpoint owner in the current rollback stack."""
    for index, checkpoint in enumerate(state.checkpoints):
        if checkpoint.owner is owner:
            return index
    raise ValueError("recovery checkpoint is no longer owned by this world")


def _pending_recovery_index(state: _PilotState, pending: PendingDeparture) -> int:
    """Locate the irreversible recovery floor owned by pending progress.

    The opening rollback receipt remains the corridor fallback only until the
    departure banks a newer target-progress receipt.  Once present, that saved
    owner is the floor for destructive recovery as well as expiry; resolving
    the owner here also picks up any later refresh of its executable artifact.
    """
    owner = pending.saved_progress_owner or pending.rollback_owner
    return _checkpoint_index(state, owner)


def _refresh_checkpoint(existing: _Checkpoint, receipt: _Checkpoint) -> _Checkpoint:
    """Refresh one stack slot without transferring its rollback ownership."""
    return replace(receipt, owner=existing.owner)


def _trial_checkpoint(
    trial: _AcceptedTrial,
    state: _PilotState,
    *,
    trend: int | None = None,
) -> _Checkpoint:
    """Snapshot one accepted trial using its owned target objective."""
    verified = trial.verification
    if not isinstance(verified, AssessedMotion):
        raise ValueError("target acceptance has no trend checkpoint")
    resolved_trend = verified.trend if trend is None else trend
    return _Checkpoint(
        verified.new_key,
        state.snapshot_world(),
        resolved_trend,
        trial.attempt.bearing.objective,
    )


def _pending_departure_payload(
    pending: PendingDeparture,
    *,
    before_source_mark_fields: Mapping[str, Any] | None = None,
    after_source_mark_fields: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Shared pending-departure fields with stable transcript key order."""
    return {
        "channel_tag": pending.opening.channel_tag,
        "from_value": pending.opening.from_value,
        **dict(before_source_mark_fields or {}),
        "earned_work_mark": pending.earned_work_mark,
        **dict(after_source_mark_fields or {}),
    }


def _checkpoint_recovery_origin(
    state: _PilotState,
    *,
    checkpoint_index: int = -1,
    before_snap: Mapping[str, Any] | None = None,
) -> _RecoveryOrigin:
    """Use one checkpoint as both rollback owner and incident anchor."""
    checkpoint_index %= len(state.checkpoints)
    checkpoint = state.checkpoints[checkpoint_index]
    return _RecoveryOrigin(
        checkpoint_owner=checkpoint.owner,
        anchor_scan=checkpoint.world.work.state.scan_id,
        before_snap=dict(checkpoint.world.work.state.tags if before_snap is None else before_snap),
    )


def _channel_recovery_origin(
    state: _PilotState,
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    channel_tag: str,
    channel_value: Any,
) -> _RecoveryOrigin:
    """Return the owner and incident anchor for the current channel tenure.

    Target-relative progress may bank several checkpoints while an outer
    operation remains on the same channel value.  A later departure belongs to
    that whole continuous tenure: selecting only the newest progress checkpoint
    would discard earlier changed-write evidence (for example a watchdog that
    fired before a nested timer boundary completed).
    """
    index = len(state.checkpoints) - 1
    while index > 0:
        previous = state.checkpoints[index - 1]
        if not _values_match(
            previous.world.work.state.tags.get(channel_tag),
            channel_value,
        ):
            break
        index -= 1
    checkpoint = state.checkpoints[index]
    checkpoint_snap = dict(checkpoint.world.work.state.tags)
    # If the tenure receipt precedes the channel state this coast launched
    # from, replay must include earlier motion, including the action that armed
    # the fault. Using the post-action frame as "before" would already contain
    # alarm triggers and erase the counterfactual evidence that a permissive
    # clears them.
    pulse = trial.attempt.pulse
    replay_from_checkpoint = (
        checkpoint.world.work.state.scan_id < pulse.scan_before
        or not _values_match(checkpoint_snap.get(channel_tag), channel_value)
    )
    return _RecoveryOrigin(
        checkpoint_owner=checkpoint.owner,
        anchor_scan=(
            checkpoint.world.work.state.scan_id if replay_from_checkpoint else pulse.scan_before
        ),
        before_snap=(checkpoint_snap if replay_from_checkpoint else dict(frame.snap)),
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
    # it still passes through the ordinary trend,
    # regression, investigation, and retry machinery below. In particular, an
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
    # missing hold and retry from the corrected PilotRungs world.
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
    # Classify BEFORE investigating (departure.py): program-owned motion may
    # preserve earned work and offer a clean forward route. Reverting
    # it would throw away the whole march, and investigation would honestly
    # confirm nothing. Affirmative clean-route evidence opens bounded pending
    # piloting; regression or unknown evidence follows the conservative
    # investigate-and-revert arm.
    observation, settled_work = observe_departure(
        state,
        ctx,
        bearing.objective,
        chan,
        departed_from,
        execution.before_snap,
        occurrence_scan=next(
            (
                event.scan
                for event in execution.timeline
                if any(
                    tag == chan
                    and _values_match(before, departed_from)
                    and not _values_match(after, departed_from)
                    for tag, before, after in event.transitions
                )
            ),
            state.work.state.scan_id,
        ),
    )
    departure = classify_departure(observation)
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


def _bearing_satisfied(trial: _AcceptedTrial) -> bool:
    """Whether trial verification proved the requested channel value."""
    return trial.execution.channel_motion.reached


def _anchor_frame_receipt(
    frame: _IterationFrame,
    state: _PilotState,
    objective: BearingObjective,
) -> int:
    """Capture the executable source world and its owned target objective."""
    key = (
        _pilot_world_key(frame.snap, state.key_config, state.pilot_rungs)
        if state.key_config is not None
        else frame.key
    )
    receipt = _Checkpoint(
        key,
        state.snapshot_world(),
        frame.distance_before,
        objective,
    )
    if state.checkpoints and state.checkpoints[-1].key == key:
        state.checkpoints[-1] = _refresh_checkpoint(state.checkpoints[-1], receipt)
    else:
        state.checkpoints.append(receipt)
    return len(state.checkpoints) - 1


def _anchor_bearing_receipt(
    trial: _AcceptedTrial,
    frame: _IterationFrame,
    state: _PilotState,
) -> None:
    """Capture the world immediately before a satisfied channel bearing.

    A route landing may expose a very different trace distance scale. If later
    motion ejects, investigation must
    replay from the state that launched the edge (Production/Idle before
    ``C_Start``), not from whichever older trend checkpoint happens to be on
    the stack (often cold Aborted).  Capture that source world before commit;
    the ordinary checkpoint/revert machinery owns it from then on.
    """
    if not _bearing_satisfied(trial):
        return
    _anchor_frame_receipt(frame, state, trial.attempt.bearing.objective)


def _open_pending_departure(
    departure: DepartureResult,
    settled_work: PLC,
    trial: _AcceptedTrial,
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[PilotEvent, ...]:
    """Record a clean departure whose progress is not yet conclusive."""
    observation = departure.observation
    channel_motion = trial.execution.channel_motion
    earned_work = state.earned_work
    # The exact pre-coast world remains the replay/rollback receipt. Settle the
    # landing before marking progress: movement completed by the departing
    # operation belongs to that operation, not to the state it happened to land
    # in. Only work earned after PILOT can read the departed world may validate
    # staying there.
    _adopt_settled_world(settled_work, state)
    earned_work_mark = (
        earned_work.mark(dict(state.work.state.tags))
        if earned_work is not None and earned_work.components
        else ()
    )
    search_scans = state.search_scans
    state.pending_departure = PendingDeparture(
        opening=observation,
        earned_work_mark=earned_work_mark,
        rollback_owner=state.checkpoints[-1].owner,
        expires_at_search_scan=min(
            ctx.max_scans,
            search_scans + _PENDING_DEPARTURE_SCAN_BUDGET,
        ),
    )
    return (
        PilotEvent(
            "pending_departure_started",
            state.work.state.scan_id,
            {
                "channel_tag": observation.channel_tag,
                "from_value": observation.from_value,
                "requested_value": channel_motion.target_value,
                "settled_value": observation.settled_value,
                "reason": departure.reason,
                "settle_scans": observation.landing_receipt.logical_scans,
                "earned_work_mark": earned_work_mark,
                "entry_progress": observation.progress,
                "classification": departure.classification.value,
            },
        ),
    )


def _adopt_settled_world(settled_work: PLC, state: _PilotState) -> None:
    """Adopt an observed settled landing without changing pending policy.

    Settlement is evidence shared by both a newly-opened pending departure and
    an already-open one that retained an unresolved departure. Keeping this
    operation separate prevents ``_open_pending_departure`` from becoming the only
    way to consume the settled fork.
    """
    scan_before = state.work.state.scan_id
    # Rebuild the overlay from the canonical rung list before adopting the
    # settled fork as the working PLC.
    _set_pilot_rungs(settled_work, state.pilot_rungs)
    state.work = settled_work
    state.dwell_scans += settled_work.state.scan_id - scan_before
    if state.steps:
        # The coast + settlement is one dwell: extend the recorded step's span
        # to the settled landing (mirrors the finished-arm rewrite).
        state.extend_last_step(settled_work.state.scan_id)


def _bank_pending_landing(trial: _AcceptedTrial, state: _PilotState) -> None:
    """Keep a local recovery receipt inside an existing pending departure.

    Retaining an investigated departure is not evidence of earned target
    progress, so this does not move ``best_trend`` or close the pending state. It
    records the actual first landing solely as the rollback/incident anchor for
    the next recomputed operation. Promotion or expiry restores the exact
    checkpoint receipts owned by ``PendingDeparture``.
    """
    verified = trial.verification
    if isinstance(verified, TargetReached):
        return
    receipt = _trial_checkpoint(trial, state)
    if state.checkpoints and state.checkpoints[-1].key == verified.new_key:
        state.checkpoints[-1] = _refresh_checkpoint(state.checkpoints[-1], receipt)
    else:
        state.checkpoints.append(receipt)


def _record_pending_landing(
    frame: _IterationFrame,
    state: _PilotState,
) -> tuple[PilotEvent, ...]:
    """Record the first recomputed world after opening a pending departure."""
    pending = state.pending_departure
    if (
        pending is None
        or pending.saved_progress_owner is not None
        or not state.checkpoints
        or state.checkpoints[-1].owner is not pending.rollback_owner
    ):
        return ()
    earned_work = state.earned_work
    progress = pending.opening.progress
    if progress.movement not in {EarnedWorkMovement.FORWARD, EarnedWorkMovement.BACKWARD}:
        progress = (
            earned_work.receipt(dict(pending.earned_work_mark), frame.snap)
            if earned_work is not None
            else EarnedWorkReceipt()
        )
    receipt = _Checkpoint(
        frame.key,
        state.snapshot_world(),
        frame.distance_before,
        state.checkpoints[-1].objective,
    )
    state.checkpoints.append(receipt)
    state.best_trend = frame.distance_before
    if progress.movement is EarnedWorkMovement.FORWARD:
        landing_mark = earned_work.mark(frame.snap) if earned_work is not None else ()
        # Save the work without closing the pending departure. The Held
        # checkpoint is now the rollback floor, while pending state gives the next
        # Unhold/rejoin transaction its ordinary local recovery semantics.
        state.pending_departure = replace(
            pending,
            earned_work_mark=landing_mark,
            saved_progress_owner=receipt.owner,
        )
        return (
            PilotEvent(
                "pending_departure_promoted",
                state.work.state.scan_id,
                _pending_departure_payload(
                    pending,
                    after_source_mark_fields={
                        "entry_progress": pending.opening.progress,
                        "landing_mark": landing_mark,
                        "trend": frame.distance_before,
                        "checkpoint_count": len(state.checkpoints),
                        "corridor_open": True,
                    },
                ),
            ),
        )
    return ()


def _assess_pending_departure(
    trial: _AcceptedTrial,
    state: _PilotState,
    ctx: _PilotContext,
) -> DepartureDecision:
    """Decide a pending departure from current progress evidence.

    Forward promotes immediately. Backward is a proven regression and enters
    the ordinary investigation/revert arm. A same-mark return to the departed
    channel's source is recovery evidence: investigate the round trip before
    later progress can retroactively promote it. Other preserved or
    incomparable evidence waits until the bounded attempt expires; expiration
    rolls back but creates no regression nogood.
    """
    pending = state.pending_departure
    assert pending is not None
    now_snap = trial.execution.after_snap or {}
    reached = target_reached(
        now_snap,
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    )
    earned_work = state.earned_work
    anchor = dict(pending.earned_work_mark)
    progress = (
        earned_work.receipt(anchor, now_snap) if earned_work is not None else EarnedWorkReceipt()
    )
    # Earned work may advance on the same scan that a pilot act drives the machine
    # into a worse target-relative world (for example, a recipe step increments
    # while an unsafe Unhold enters Aborted). Trial attribution is the narrower
    # causal fact and must win over that incidental ordinal movement.
    verified = trial.verification
    if (
        isinstance(verified, AssessedMotion)
        and verified.assessment.agency is Agency.PILOT
        and verified.assessment.bearing is BearingEffect.DEPARTED
        and verified.assessment.progress is ProgressEffect.BACKWARD
    ):
        caused_regression = True
    else:
        caused_regression = False
    if reached:
        return DepartureDecision(DepartureAction.PROMOTE, progress)
    if caused_regression:
        return DepartureDecision(
            DepartureAction.REGRESS,
            progress,
            DepartureBasis.PILOT_CAUSED_REGRESSION,
        )
    if progress.movement is EarnedWorkMovement.BACKWARD:
        return DepartureDecision(DepartureAction.REGRESS, progress)
    if progress.movement is EarnedWorkMovement.FORWARD:
        return DepartureDecision(DepartureAction.PROMOTE, progress)
    if state.search_scans < pending.expires_at_search_scan:
        return DepartureDecision(DepartureAction.WAIT, progress)
    return DepartureDecision(DepartureAction.EXPIRE, progress)


def _departure_event_outcome(decision: DepartureDecision) -> str:
    """Render the decision's target-relative movement evidence."""
    if decision.basis is DepartureBasis.PILOT_CAUSED_REGRESSION:
        return EarnedWorkMovement.BACKWARD.value
    return decision.receipt.movement.value


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


def _install_confirmed_correction(
    state: _PilotState,
    correction: _ConfirmedCorrection,
    *,
    origin_key: tuple[Any, ...],
    scan: int,
    source: str,
) -> _CorrectionReceipt:
    """Install one locally replay-proven correction on probation.

    A correction is installed only in the exact guarded form that survived
    replay, and only one competing explanation is installed for an incident.
    The checks below reject forged identities and already-owned rungs;
    prerequisite installation reuses an identical rung without claiming it.
    Installation banks active corrections into every revert anchor, and
    revocation removes them symmetrically.
    """
    if not correction.pilot_rungs:
        raise ValueError("a confirmed correction must own at least one rung")
    if any(not isinstance(rung, PilotRung) for rung in correction.pilot_rungs):
        raise TypeError("a confirmed correction may contain only executable PilotRungs")
    if correction.identity != correction_identity(correction.pilot_rungs):
        raise ValueError("confirmed correction identity does not match its replayed rungs")
    correction_rung_ids = tuple(_rung_identity(rung) for rung in correction.pilot_rungs)
    if len(set(correction_rung_ids)) != len(correction_rung_ids):
        raise ValueError("a confirmed correction cannot contain duplicate rungs")
    existing = {_rung_identity(rung) for rung in state.pilot_rungs}
    duplicate = tuple(rung for rung in correction.pilot_rungs if _rung_identity(rung) in existing)
    if duplicate:
        raise ValueError(
            "confirmed correction cannot claim already-owned rung(s): "
            f"{tuple((rung.dest, rung.value) for rung in duplicate)!r}"
        )
    state.pilot_rungs = _append_pilot_rungs(
        state.work,
        list(correction.pilot_rungs),
        state.pilot_rungs,
    )
    state.hold_log.append(
        _HoldLogEntry(
            scan=scan,
            source=source,
            pilot_rungs=correction.pilot_rungs,
        )
    )
    receipt = _CorrectionReceipt(
        receipt_id=(
            max(
                (existing.receipt_id for existing in state.correction_receipts),
                default=0,
            )
            + 1
        ),
        origin_key=origin_key,
        correction=correction,
    )
    state.correction_receipts.append(receipt)
    key_config = state.key_config
    banked: list[_Checkpoint] = []
    for checkpoint in state.checkpoints:
        existing_ids = {_rung_identity(rung) for rung in checkpoint.world.pilot_rungs}
        checkpoint_pilot_rungs = [*checkpoint.world.pilot_rungs]
        checkpoint_pilot_rungs.extend(
            rung for rung in correction.pilot_rungs if _rung_identity(rung) not in existing_ids
        )
        banked.append(_checkpoint_with_pilot_rungs(checkpoint, checkpoint_pilot_rungs, key_config))
    state.checkpoints = banked
    return receipt


def _promote_probationary_corrections(state: _PilotState) -> tuple[int, ...]:
    """Promote installed hypotheses after the live run banks real progress."""
    promoted = tuple(
        receipt.receipt_id
        for receipt in state.correction_receipts
        if receipt.status is CorrectionStatus.PROBATIONARY
    )
    if promoted:
        promoted_ids = set(promoted)
        state.correction_receipts = [
            replace(receipt, status=CorrectionStatus.ACTIVE)
            if receipt.receipt_id in promoted_ids
            else receipt
            for receipt in state.correction_receipts
        ]
    return promoted


def _checkpoint_with_pilot_rungs(
    checkpoint: _Checkpoint,
    pilot_rungs: list[PilotRung],
    key_config: Any,
) -> _Checkpoint:
    """Return one checkpoint re-keyed around an exact executable overlay."""
    if tuple(pilot_rungs) == tuple(checkpoint.world.pilot_rungs):
        return checkpoint
    work = fork_with_pilot_rungs(checkpoint.world.work, pilot_rungs)
    world = checkpoint.world.set(work=work, pilot_rungs=pvector(pilot_rungs))
    key = (
        _pilot_world_key(dict(work.state.tags), key_config, pilot_rungs)
        if key_config is not None
        else checkpoint.key
    )
    return replace(checkpoint, key=key, world=world)


def _contradicted_corrections(
    state: _PilotState,
    investigation: InvestigationResult,
    snapshot: Mapping[str, Any],
) -> tuple[_CorrectionReceipt, ...]:
    """Overlay-effective corrections contradicted by the exact new remedy.

    A later hypothesis that causally names an installed destination and needs a
    value outside the correction's admitted values is evidence that the prior
    correction caused this regression.  Treating the opposite value as another
    durable hold would leave two tools arguing in the overlay.
    """
    correction = investigation.correction
    if correction is None:
        return ()
    sources = set(correction.sources)
    remedy_rungs: dict[str, list[tuple[Any, Any]]] = {}
    for rung in correction.pilot_rungs:
        remedy_rungs.setdefault(rung.dest, []).append((rung.value, rung.operation))

    def _compatible_phases(new_operation: Any, old: PilotRung) -> bool:
        """Opposite values with distinct owner boundaries are temporal phases."""
        return (
            new_operation is not None
            and old.operation is not None
            and _semantic_key(new_operation.until) != _semantic_key(old.operation.until)
        )

    effective_ids = {
        _rung_identity(rung)
        for rung in _pilot_rung_execution_receipt(state.pilot_rungs, snapshot).effective
    }
    contradicted: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if not receipt.status.effective:
            continue
        admitted: dict[str, list[PilotRung]] = {}
        for rung in receipt.pilot_rungs:
            if _rung_identity(rung) in effective_ids:
                admitted.setdefault(rung.dest, []).append(rung)
        if any(
            tag in sources
            and all(
                not any(
                    _values_match(remedy_value, old.value)
                    or _compatible_phases(remedy_operation, old)
                    for old in admitted.get(tag, ())
                )
                for remedy_value, remedy_operation in rungs
            )
            for tag, rungs in remedy_rungs.items()
            if tag in admitted
        ):
            contradicted.append(receipt)
    return tuple(contradicted)


def _causally_harmful_corrections(
    state: _PilotState,
    witness: RegressionWitness | None,
    snapshot: Mapping[str, Any],
) -> tuple[_CorrectionReceipt, ...]:
    """Effective corrections whose exact active PILOT write caused this incident.

    Banking progress promotes confidence; it does not make a synthetic rung
    immune to later causal testimony. If a later incident's recorded
    ``cause()`` chain contains the exact active write owned by any effective
    correction, the live machine has supplied a counterexample and the rung
    must be removed even when investigation cannot yet name a replacement.

    Match the exact PILOT write (destination and value), then consume the
    execution layer's effective-owner receipt. This avoids blaming a dormant,
    eligible, continuing-but-overridden, or shadowed correction that merely
    mentions the same tag. Guard ownership is evaluated in the
    witness's pre-departure world, not the earlier incident anchor: a delayed
    correction may become active only shortly before it causes harm.
    """
    if witness is None:
        return ()
    causal_values = (
        tuple(
            (occurrence.tag, occurrence.value)
            for occurrence in witness.cause
            if occurrence.rung.subroutine == "PILOT"
        )
        + witness.causal_roots
    )
    if not causal_values:
        return ()

    overlay = _pilot_rung_execution_receipt(
        state.pilot_rungs,
        dict(witness.owner_snapshot or snapshot),
    )
    active_owner = {rung.dest: rung for rung in overlay.effective}

    harmful: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if not receipt.status.effective:
            continue
        owns_cause = False
        for rung in receipt.pilot_rungs:
            owner = active_owner.get(rung.dest)
            if owner is None or _rung_identity(owner) != _rung_identity(rung):
                continue
            if any(
                tag == rung.dest and _values_match(value, rung.value)
                for tag, value in causal_values
            ):
                owns_cause = True
                break
        if owns_cause:
            harmful.append(receipt)
    return tuple(harmful)


def _revoke_corrections(
    state: _PilotState,
    receipts: tuple[_CorrectionReceipt, ...],
) -> tuple[int, ...]:
    """Revoke causally harmful receipts and rebuild the checkpoint without them."""
    if not receipts:
        return ()
    receipt_ids = {receipt.receipt_id for receipt in receipts}
    revoked_rung_ids = {
        _rung_identity(rung) for receipt in receipts for rung in receipt.pilot_rungs
    }
    state.correction_receipts = [
        replace(receipt, status=CorrectionStatus.REVOKED)
        if receipt.receipt_id in receipt_ids
        else receipt
        for receipt in state.correction_receipts
    ]
    for receipt in receipts:
        state.correction_nogoods.setdefault(receipt.origin_key, set()).add(receipt.identity)
        state.hold_log.append(
            _HoldLogEntry(
                scan=state.work.state.scan_id,
                source="revocation",
                pilot_rungs=receipt.pilot_rungs,
            )
        )

    remaining_pilot_rungs = [
        rung for rung in state.pilot_rungs if _rung_identity(rung) not in revoked_rung_ids
    ]
    state.pilot_rungs = remaining_pilot_rungs
    _set_pilot_rungs(state.work, remaining_pilot_rungs)
    key_config = state.key_config
    cleaned_checkpoints: list[_Checkpoint] = []
    for saved in state.checkpoints:
        saved_pilot_rungs = [
            rung for rung in saved.world.pilot_rungs if _rung_identity(rung) not in revoked_rung_ids
        ]
        cleaned_checkpoints.append(
            _checkpoint_with_pilot_rungs(saved, saved_pilot_rungs, key_config)
        )
    state.checkpoints = cleaned_checkpoints
    restored_key = state.checkpoints[-1].key
    # The same machine tags now carry different correction knowledge. Permit
    # the retry; the revoked correction identity will be excluded explicitly.
    state.seen_keys.discard(restored_key)
    return tuple(sorted(receipt_ids))


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
        bearing = _deviation_bearing(
            execution,
            frame,
            state.watch_tags,
            bearing_owner.objective.frontier,
        )
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

        # Replay re-arms each step's recorded session spec (kind + channel +
        # target) from the committed step context.
        replay_steps = tuple(
            _replay_step(step, act.context)
            for act in state.committed_acts
            for step in act.steps
            if step.scan_before >= cp_fork.state.scan_id
        )
        role_tags = coast_departure_tags(state, ctx)
        regression_witness = incident_regression_witness(pulse.fork, incident)
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
            "hypotheses": len(investigation.hypotheses),
            "confirmed": len(investigation.confirmed),
            "rejected": len(investigation.rejected),
            "unresolved": investigation.unresolved,
            "hypothesis_detail": tuple(_hyp_detail(h) for h in investigation.hypotheses),
            "confirmed_detail": tuple(_hyp_detail(h) for h in investigation.confirmed),
            "rejected_detail": tuple(_rejection_detail(r) for r in investigation.rejected),
            "revoked_corrections": tuple(receipt.receipt_id for receipt in revoked_receipts),
        }
    if retain_if_unresolved is not None and confirmed_correction is None and not revoked_receipts:
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
    if regression_nogoods:
        ctx.compass, _ = ctx.compass.apply(
            tuple(ActionNogoodObservation(frame.key, ("pair", pair)) for pair in regression_nogoods)
        )
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
