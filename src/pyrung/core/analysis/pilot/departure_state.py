"""Own pending-departure state, checkpoints, and pure assessments.

This module records departure policy state and performs its local checkpoint,
settlement, and earned-work bookkeeping. It does not stream monitor events,
investigate a regression, choose a corrective hypothesis, or apply the final
departure decision.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.departure import (
    DepartureObservation,
    DepartureResult,
)
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWorkMovement,
    EarnedWorkReceipt,
)
from pyrung.core.analysis.pilot.navigation_contracts import BearingObjective
from pyrung.core.analysis.pilot.outcome import Agency, BearingEffect, ProgressEffect
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    PilotEvent,
    TargetReached,
    _AcceptedTrial,
    _Checkpoint,
    _CheckpointOwner,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _RecoveryOrigin,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
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
        _pilot_world_key(
            frame.snap,
            state.key_config,
            state.pilot_rungs,
            state.active_requirements,
        )
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
                "settle_scans": observation.logical_scans,
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
    # ``_settle_departure`` already created this fork with the current overlay;
    # it owns the settlement suffix and is the next executable world. A later
    # overlay change crosses its own boundary through ``state.pilot_rungs``.
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
