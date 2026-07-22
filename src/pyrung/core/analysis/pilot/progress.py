"""Retain, continue, or revert a committed trial world.

After a trial passes verification, this module compares target distance and
gauge marks, updates checkpoints, and classifies program-owned departures.
Regression handling builds an incident, replay-validates corrective hypotheses,
installs at most one surviving correction, and restores the appropriate
checkpoint. A clean departure may remain pending until later gauge evidence
promotes it or requires rollback.

This is the owner of post-commit recovery policy, not trial execution or local
gate acceptance.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import replace
from typing import Any

from pyrsistent import pvector

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _append_rungs,
    _pilot_world_key,
    _rung_identity,
    _semantic_key,
    _set_rungs,
    coast_departure_tags,
)
from pyrung.core.analysis.pilot.compass import ActionNogoodObservation
from pyrung.core.analysis.pilot.detour import (
    DepartureVerdict,
    classify_departure,
)
from pyrung.core.analysis.pilot.investigate import (
    InvestigationRejection,
    InvestigationResult,
    RegressionWitness,
    ReplayIncident,
    ReplayStep,
    build_deviation_incident,
    build_replay_fn,
    correction_identity,
    incident_regression_witness,
    investigate_deviation,
)
from pyrung.core.analysis.pilot.navigation import BearingObjective
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    Outcome,
    ProgressEffect,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    CorrectionStatus,
    DepartureAction,
    DepartureDecision,
    MotionKind,
    PendingDeparture,
    PilotEvent,
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
    _Step,
    _StepContext,
    _TrialResult,
)
from pyrung.core.analysis.sp_values import _SnapshotView, _values_match

_PENDING_DEPARTURE_SCAN_BUDGET = 2000


def _checkpoint_index(state: _PilotState, owner: _CheckpointOwner) -> int:
    """Locate an exact checkpoint owner in the current rollback stack."""
    for index, checkpoint in enumerate(state.checkpoints):
        if checkpoint.owner is owner:
            return index
    raise ValueError("recovery checkpoint is no longer owned by this world")


def _refresh_checkpoint(existing: _Checkpoint, receipt: _Checkpoint) -> _Checkpoint:
    """Refresh one stack slot without transferring its rollback ownership."""
    return replace(receipt, owner=existing.owner)


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
    trial: _TrialResult,
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
    replay_from_checkpoint = (
        checkpoint.world.work.state.scan_id < trial.scan_before
        or not _values_match(checkpoint_snap.get(channel_tag), channel_value)
    )
    return _RecoveryOrigin(
        checkpoint_owner=checkpoint.owner,
        anchor_scan=(
            checkpoint.world.work.state.scan_id if replay_from_checkpoint else trial.scan_before
        ),
        before_snap=(checkpoint_snap if replay_from_checkpoint else dict(frame.snap)),
    )


def _investigation_started_event(
    trial: _TrialResult,
    origin: _RecoveryOrigin,
) -> PilotEvent:
    """Announce expensive causal replay before it starts."""
    channel_tag = trial.zoom_channel_tag
    return PilotEvent(
        "investigation_started",
        trial.fork.state.scan_id,
        {
            "channel_tag": channel_tag,
            "from_value": (
                origin.before_snap.get(channel_tag) if channel_tag is not None else None
            ),
            "to_value": (trial.fork_snap.get(channel_tag) if channel_tag is not None else None),
            "action": trial.applied,
        },
    )


def _monitor_trend(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> Iterator[PilotEvent]:
    channel_ejection = (
        trial.zoom_channel_tag is not None
        and (
            trial.assessment is not None
            and trial.assessment.bearing is BearingEffect.DEPARTED
            or trial.assessment is None
            and trial.outcome == Outcome.AMBIENT_DRIFT
        )
        and not _values_match(
            trial.fork_snap.get(trial.zoom_channel_tag),
            trial.before_snap.get(trial.zoom_channel_tag),
        )
    )
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

    if trial.new_key is None or trial.trend is None:
        return

    assert state.best_trend is not None

    # A FRONTIER outcome means the pilot knowingly exposed a world with
    # more prerequisites.  Commit the observation, but keep the previous
    # checkpoint and high-water mark alive: if the new world keeps drifting
    # away, the next verify pass should revert to the pre-frontier checkpoint
    # and chase the PLC-side cause.
    if trial.outcome == Outcome.FRONTIER:
        yield PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": trial.trend,
                "key": trial.new_key,
                "checkpoint_count": len(state.checkpoints),
                "frontier": True,
                "baseline_trend": state.best_trend,
            },
        )
        return

    # A coast that *ejected* — the macro-state left the value it was held at
    # and wandered into a side branch (Execute -> Holding/Aborting). Route
    # zoom and terminal let-run use the same evidence and rollback mechanics.
    # That branch's trace distance is misleadingly LOW (fewer open leaves than the
    # held state), so the ordinary ``trend < best_trend`` test below would
    # checkpoint the ejection as progress.  It is not progress: the watchdog that
    # ejected fired *during the coast*, not after it.  Investigate over the
    # coast-span window (the fork's own history, ``scan_before -> fork end``) so
    # its exact channel-transition producer and upstream corrective levers are
    # recoverable, then revert to the pre-coast checkpoint.
    if channel_ejection:
        chan = trial.zoom_channel_tag
        assert chan is not None
        departed_from = trial.before_snap.get(chan)
        investigated = bool(state.checkpoints)
        ejection = PilotEvent(
            "letrun_ejection",
            state.work.state.scan_id,
            {
                "channel_tag": chan,
                "from_value": departed_from,
                "requested_value": trial.zoom_target_value,
                "to_value": trial.fork_snap.get(chan),
                "observe_label": trial.observe_label,
                "coast_span": (trial.scan_before, state.work.state.scan_id),
                "investigated": investigated,
                "reason": None if investigated else "no checkpoint to revert to",
            },
        )
        if not investigated:
            # No prior checkpoint to anchor the incident or revert to — the
            # ejected state stands committed.  Surface why so the bail is visible
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
                "to_value": trial.fork_snap.get(chan),
            },
        )
        # Classify BEFORE investigating (detour.py): program-owned motion may
        # preserves the progress gauge and offers a clean forward route —
        # reverting it would throw away the whole march, and investigation
        # would honestly confirm nothing. Affirmative clean-route evidence opens
        # bounded pending piloting; regression or unknown evidence follows
        # the conservative investigate-and-revert arm.
        verdict = classify_departure(
            state,
            ctx,
            trial.bearing_objective,
            chan,
            departed_from,
            trial.before_snap,
        )
        if verdict.can_continue:
            prescribed_departure = (
                trial.route_prescribed
                and trial.assessment is not None
                and trial.assessment.agency is Agency.PILOT
            )
            if verdict.progress.effect == "preserved" and not prescribed_departure:
                # A clean route says the landing is usable, but a known-
                # preserved progress receipt says this occurrence earned
                # no program work. For ambient motion it may therefore be
                # a preventable ejection. A Compass/current edge earns
                # tide-table credit only when causal attribution says the
                # pilot actually produced this departure; program-caused
                # motion encountered during a prescribed coast remains
                # ambient.
                #
                # This decision is occurrence-local. An already-open
                # pending state changes only the rollback boundary; it
                # must not suppress understanding the same physical departure.
                # Investigation therefore runs before retention in both cases.
                origin = _channel_recovery_origin(
                    state,
                    trial,
                    frame,
                    chan,
                    departed_from,
                )
                if trial.chase_regression_causes:
                    yield _investigation_started_event(trial, origin)
                yield from _investigate_and_revert(
                    trial,
                    frame,
                    state,
                    ctx,
                    origin=origin,
                    retain_if_unresolved=verdict,
                )
                return
            if state.pending_departure is None:
                yield from _open_pending_departure(verdict, trial, state, ctx, chan)
                return
            # A clean program-owned departure inside an existing bounded
            # attempt that earned work (or fulfilled an explicitly prescribed
            # channel transaction) is ordinary piloting. Keep the original
            # rollback boundary and budget; do not nest another pending departure.
            return
        origin = _channel_recovery_origin(
            state,
            trial,
            frame,
            chan,
            departed_from,
        )
        if trial.chase_regression_causes:
            yield _investigation_started_event(trial, origin)
        yield from _investigate_and_revert(
            trial,
            frame,
            state,
            ctx,
            origin=origin,
        )
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
    if _bearing_satisfied(trial) and trial.trend > state.best_trend:
        assert trial.zoom_channel_tag is not None
        channel_tag = trial.zoom_channel_tag
        previous = state.best_trend
        state.best_trend = trial.trend
        yield PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": trial.trend,
                "key": trial.new_key,
                "checkpoint_count": len(state.checkpoints),
                "channel": channel_tag,
                "channel_value": trial.fork_snap.get(channel_tag),
                "baseline_trend": previous,
                "provisional": True,
            },
        )
        return

    if trial.trend < state.best_trend:
        if state.pending_departure is not None:
            promoted = _apply_departure_decision(
                DepartureDecision(DepartureAction.PROMOTE, "banked ordinary progress"),
                trial,
                frame,
                state,
                ctx,
            )
            assert promoted is not None
            yield PilotEvent(
                "trend_checkpoint",
                state.work.state.scan_id,
                {
                    "trend": state.best_trend,
                    "key": trial.new_key,
                    "checkpoint_count": len(state.checkpoints),
                },
            )
            yield from promoted
            return
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.bearing_objective,
            )
        )
        state.best_trend = trial.trend
        promoted_corrections = _promote_probationary_corrections(state)
        checkpoint_event = PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": state.best_trend,
                "key": trial.new_key,
                "checkpoint_count": len(state.checkpoints),
                "promoted_corrections": promoted_corrections,
            },
        )
        yield checkpoint_event
        return

    if trial.trend == state.best_trend and trial.outcome == Outcome.CONFIRMED:
        state.checkpoints.append(
            _Checkpoint(
                trial.new_key,
                state.snapshot_world(),
                trial.trend,
                trial.bearing_objective,
            )
        )
        yield PilotEvent(
            "trend_checkpoint",
            state.work.state.scan_id,
            {
                "trend": state.best_trend,
                "key": trial.new_key,
                "checkpoint_count": len(state.checkpoints),
                "flat": True,
            },
        )
        return

    if trial.trend <= state.best_trend or not state.checkpoints:
        return

    origin = _checkpoint_recovery_origin(state, before_snap=frame.snap)
    if trial.chase_regression_causes:
        yield _investigation_started_event(trial, origin)
    yield from _investigate_and_revert(
        trial,
        frame,
        state,
        ctx,
        origin=origin,
    )


def _bearing_satisfied(trial: _TrialResult) -> bool:
    """Whether trial verification proved the requested channel value."""
    if trial.zoom_channel_tag is None:
        return False
    if trial.assessment is not None:
        return trial.assessment.bearing is BearingEffect.SATISFIED
    return _values_match(
        trial.fork_snap.get(trial.zoom_channel_tag),
        trial.zoom_target_value,
    )


def _anchor_frame_receipt(
    frame: _IterationFrame,
    state: _PilotState,
    objective: BearingObjective,
) -> int:
    """Capture the executable source world and its owned target objective."""
    key = (
        _pilot_world_key(frame.snap, state.key_config, state.rungs)
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
    trial: _TrialResult,
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
    _anchor_frame_receipt(frame, state, trial.bearing_objective)


def _open_pending_departure(
    verdict: DepartureVerdict,
    trial: _TrialResult,
    state: _PilotState,
    ctx: _PilotContext,
    chan: str,
) -> tuple[PilotEvent, ...]:
    """Record a clean departure whose progress is not yet conclusive."""
    gauge = state.gauge
    # The exact pre-coast world remains the replay/rollback receipt. The
    # pending progress mark starts at the observed departure world so work already
    # earned during the coast is not counted a second time as side-motion
    # progress (e.g. 101->103 must not prematurely promote before 103->105).
    progress_mark = (
        gauge.mark(dict(state.work.state.tags)) if gauge is not None and gauge.components else ()
    )
    departed_from = trial.before_snap.get(chan)
    _adopt_settled_departure(verdict, state)
    search_scan = state.search_scan
    state.pending_departure = PendingDeparture(
        channel_tag=chan,
        from_value=departed_from,
        progress_mark=progress_mark,
        rollback_owner=state.checkpoints[-1].owner,
        expires_at=min(ctx.max_scans, search_scan + _PENDING_DEPARTURE_SCAN_BUDGET),
        opening_progress=verdict.progress,
    )
    return (
        PilotEvent(
            # Stable diagnostic vocabulary retained for existing consumers.
            "provisional_started",
            state.work.state.scan_id,
            {
                "channel_tag": chan,
                "from_value": departed_from,
                "requested_value": trial.zoom_target_value,
                "settled_value": verdict.settled_value,
                "reason": verdict.reason,
                "route": verdict.route,
                "settle_scans": verdict.settle_scans,
                "gauge_at_source": progress_mark,
                "entry_progress": verdict.progress,
                "classification": verdict.decision,
            },
        ),
    )


def _adopt_settled_departure(verdict: DepartureVerdict, state: _PilotState) -> int:
    """Adopt the classifier's settled landing without changing pending policy.

    Settlement is evidence shared by both a newly-opened pending departure and
    an already-open one that retained an unresolved departure. Keeping this
    operation separate prevents ``_open_pending_departure`` from becoming the only
    way to consume the settled fork.
    Returns the scan at which adoption began.
    """
    settled = verdict.settled_fork
    scan_before = state.work.state.scan_id
    # Rebuild the overlay from the canonical rung list before adopting the
    # settled fork as the working PLC.
    _set_rungs(settled, state.rungs)
    state.work = settled
    state.dwell_scans += settled.state.scan_id - scan_before
    if state.steps:
        # The coast + settlement is one dwell: extend the recorded step's span
        # to the settled landing (mirrors the finished-arm rewrite).
        state.extend_last_step(settled.state.scan_id)
    return scan_before


def _bank_pending_landing(trial: _TrialResult, state: _PilotState) -> None:
    """Keep a local recovery receipt inside an existing pending departure.

    Retaining an investigated departure is not evidence of earned target
    progress, so this does not move ``best_trend`` or close the pending state. It
    records the actual first landing solely as the rollback/incident anchor for
    the next recomputed operation. Promotion or expiry restores the exact
    checkpoint receipts owned by ``PendingDeparture``.
    """
    if trial.new_key is None or trial.trend is None:
        return
    receipt = _Checkpoint(
        trial.new_key,
        state.snapshot_world(),
        trial.trend,
        trial.bearing_objective,
    )
    if state.checkpoints and state.checkpoints[-1].key == trial.new_key:
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
    gauge = state.gauge
    outcome = pending.opening_progress.effect
    if outcome not in {"advanced", "behind"}:
        outcome = (
            gauge.compare(dict(pending.progress_mark), frame.snap)
            if gauge is not None and gauge.components
            else "unknown"
        )
    receipt = _Checkpoint(
        frame.key,
        state.snapshot_world(),
        frame.distance_before,
        state.checkpoints[-1].objective,
    )
    state.checkpoints.append(receipt)
    state.best_trend = frame.distance_before
    if outcome == "advanced":
        landing_mark = gauge.mark(frame.snap) if gauge is not None else ()
        # Save the work without closing the pending departure. The Held
        # checkpoint is now the rollback floor, while pending state gives the next
        # Unhold/rejoin transaction its ordinary local recovery semantics.
        state.pending_departure = replace(
            pending,
            progress_mark=landing_mark,
            saved_progress_owner=receipt.owner,
        )
        return (
            PilotEvent(
                # Stable diagnostic vocabulary retained for existing consumers.
                "provisional_promoted",
                state.work.state.scan_id,
                {
                    "channel_tag": pending.channel_tag,
                    "from_value": pending.from_value,
                    "gauge_at_source": pending.progress_mark,
                    "entry_progress": pending.opening_progress,
                    "landing_mark": landing_mark,
                    "trend": frame.distance_before,
                    "checkpoint_count": len(state.checkpoints),
                    "corridor_open": True,
                },
            ),
        )
    return ()


def _assess_pending_departure(
    trial: _TrialResult,
    state: _PilotState,
    ctx: _PilotContext,
) -> DepartureDecision:
    """Decide a pending departure from current progress evidence.

    Advanced promotes immediately. Behind is a proven regression and enters
    the ordinary investigation/revert arm. Preserved or incomparable evidence
    waits until the bounded attempt expires; expiration rolls back but
    creates no regression nogood.
    """
    pending = state.pending_departure
    assert pending is not None
    now_snap = trial.fork_snap or {}
    reached = target_reached(
        now_snap,
        ctx.target_tag,
        ctx.target_value,
        ctx.target_predicate,
    )
    gauge = state.gauge
    anchor = dict(pending.progress_mark)
    outcome = (
        gauge.compare(anchor, now_snap) if gauge is not None and gauge.components else "unknown"
    )
    # A gauge may advance on the same scan that a pilot act drives the machine
    # into a worse target-relative world (for example, a recipe step increments
    # while an unsafe Unhold enters Aborted). Trial attribution is the narrower
    # causal fact and must win over that incidental ordinal movement.
    if (
        trial.assessment is not None
        and trial.assessment.agency is Agency.PILOT
        and trial.assessment.bearing is BearingEffect.DEPARTED
        and trial.assessment.progress is ProgressEffect.BEHIND
    ):
        outcome = "behind"
    if outcome == "advanced" or reached:
        return DepartureDecision(DepartureAction.PROMOTE, outcome)
    if outcome == "behind":
        return DepartureDecision(DepartureAction.REGRESS, outcome)
    if state.search_scan < pending.expires_at:
        return DepartureDecision(DepartureAction.WAIT, outcome)
    return DepartureDecision(DepartureAction.EXPIRE, outcome)


def _apply_departure_decision(
    decision: DepartureDecision,
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> tuple[PilotEvent, ...] | None:
    """Apply one assessment to the exact receipts owned by pending state."""
    pending = state.pending_departure
    assert pending is not None
    if decision.action is DepartureAction.WAIT:
        return None

    state.pending_departure = None
    rollback_index = _checkpoint_index(state, pending.rollback_owner)
    saved_progress = (
        state.checkpoints[_checkpoint_index(state, pending.saved_progress_owner)]
        if pending.saved_progress_owner is not None
        else None
    )
    del state.checkpoints[rollback_index + 1 :]
    if decision.action is DepartureAction.PROMOTE:
        promoted_trend = trial.trend if trial.trend is not None else 0
        if trial.new_key is not None:
            state.checkpoints.append(
                _Checkpoint(
                    trial.new_key,
                    state.snapshot_world(),
                    promoted_trend,
                    trial.bearing_objective,
                )
            )
        state.best_trend = promoted_trend
        promoted_corrections = _promote_probationary_corrections(state)
        return (
            PilotEvent(
                # Stable diagnostic vocabulary retained for existing consumers.
                "provisional_promoted",
                state.work.state.scan_id,
                {
                    "channel_tag": pending.channel_tag,
                    "from_value": pending.from_value,
                    "gauge_at_source": pending.progress_mark,
                    "landing_mark": (
                        state.gauge.mark(trial.fork_snap) if state.gauge is not None else ()
                    ),
                    "outcome": decision.progress,
                    "trend": promoted_trend,
                    "checkpoint_count": len(state.checkpoints),
                    "terminal": trial.new_key is None,
                    "promoted_corrections": promoted_corrections,
                },
            ),
        )
    if decision.action is DepartureAction.REGRESS:
        event = PilotEvent(
            # Stable diagnostic vocabulary retained for existing consumers.
            "provisional_regressed",
            state.work.state.scan_id,
            {
                "channel_tag": pending.channel_tag,
                "from_value": pending.from_value,
                "outcome": decision.progress,
                "gauge_at_source": pending.progress_mark,
            },
        )
        regression = _investigate_and_revert(
            trial,
            frame,
            state,
            ctx,
            origin=_checkpoint_recovery_origin(state),
        )
        return (event, *regression)

    assert decision.action is DepartureAction.EXPIRE
    if saved_progress is not None:
        state.checkpoints.append(saved_progress)
    checkpoint = state.checkpoints[-1]
    state.load_world(checkpoint.world)
    state.best_trend = checkpoint.trend
    return (
        PilotEvent(
            # Stable diagnostic vocabulary retained for existing consumers.
            "provisional_expired",
            state.work.state.scan_id,
            {
                "channel_tag": pending.channel_tag,
                "from_value": pending.from_value,
                "outcome": decision.progress,
                "gauge_at_source": pending.progress_mark,
            },
        ),
    )


def _channel_transitions(
    ctx: _PilotContext,
    cp_fork: Any,
    regressed_snap: Any,
) -> tuple[tuple[str, Any, Any], ...]:
    """Channel-register transitions a revert undoes: ``(tag, from, to)``.

    ``from`` is the checkpoint value, ``to`` the regressed frame's value, for the
    navigated channel (the target register) when it moved.  Recording only —
    legibility so a destructive move (``S_StateCurrent 6->8`` Aborting) is
    distinguishable from useful program-owned motion (``6->11`` Held) in the
    transcript.  Scoped to the target bearing to keep the line focused; the
    derived enable/mask pipeline registers are noise here, not navigable channels.
    """
    cp_snap: Any = {}
    try:
        cp_snap = dict(getattr(cp_fork.state, "tags", {}) or {})
    except (AttributeError, TypeError):
        cp_snap = {}
    reg_snap = regressed_snap or {}

    channels: list[str] = []
    target_tag = getattr(ctx, "target_tag", None)
    if target_tag is not None:
        channels.append(target_tag)
    if not channels:
        return ()

    out: list[tuple[str, Any, Any]] = []
    for tag in channels:
        fv = cp_snap.get(tag)
        tv = reg_snap.get(tag)
        if fv is None and tv is None:
            continue
        if not _values_match(fv, tv):
            out.append((tag, fv, tv))
    return tuple(out)


def _replay_step(step: _Step, sc: _StepContext) -> ReplayStep:
    """One physical step plus its owning operation context → a replay spec.

    The kind is the RECORDED motion (pulse / zoom / letrun), never inferred
    from position or input emptiness.  A coast step with no channel register
    (the settle-path zoom) replays as a plain dwell, exactly the shape it ran
    live. Every world-side step has an owning context by construction.
    """
    inputs = tuple(step.inputs.items())
    kind = {
        MotionKind.INTERVENTION: "pulse",
        MotionKind.COAST_TO_BEARING: "zoom",
        MotionKind.COAST_HOLDING_WORLD: "letrun",
    }[sc.motion]
    if kind == "zoom" and sc.channel_tag is None:
        kind = "dwell"
    return ReplayStep(
        inputs=inputs,
        scans=step.scans,
        kind=kind,
        channel_tag=sc.channel_tag,
        channel_target=sc.channel_target,
    )


def _deviation_bearing(
    trial: _TrialResult,
    frame: _IterationFrame,
    watch_tags: list[str],
    frontier: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    """Facts the failed operation actually held and then lost.

    A zoom carries two different channel values: the source it launched from
    and the destination it requested. Only the source can be a departure
    bearing. Recording the unvisited destination here manufactures an
    impossible ``departure_scan=None`` and leaves causal ranking without the
    exact source-to-eject transition.
    """
    needed_by_tag: dict[str, list[Any]] = {}
    for tag, value in frontier:
        needed_by_tag.setdefault(tag, []).append(value)
    bearing: list[_ActionPair] = [
        (tag, frame.snap.get(tag))
        for tag in watch_tags
        if not _values_match(frame.snap.get(tag), trial.fork_snap.get(tag))
        and not any(
            _values_match(trial.fork_snap.get(tag), needed) for needed in needed_by_tag.get(tag, ())
        )
    ]
    channel = trial.zoom_channel_tag
    if channel is not None:
        source = trial.before_snap.get(channel)
        landed = trial.fork_snap.get(channel)
        if not _values_match(landed, source):
            bearing = [(tag, value) for tag, value in bearing if tag != channel]
            bearing.append((channel, source))
    return tuple(bearing)


def _install_confirmed_correction(
    state: _PilotState,
    correction: _ConfirmedCorrection,
    *,
    origin_key: tuple[Any, ...],
    scan: int,
    source: str,
) -> _CorrectionReceipt:
    """Install one locally replay-proven correction on probation."""
    if not correction.rungs:
        raise ValueError("a confirmed correction must own at least one rung")
    if any(not isinstance(rung, PilotRung) for rung in correction.rungs):
        raise TypeError("a confirmed correction may contain only executable PilotRungs")
    if correction.identity != correction_identity(correction.rungs):
        raise ValueError("confirmed correction identity does not match its replayed rungs")
    correction_rung_ids = tuple(_rung_identity(rung) for rung in correction.rungs)
    if len(set(correction_rung_ids)) != len(correction_rung_ids):
        raise ValueError("a confirmed correction cannot contain duplicate rungs")
    existing = {_rung_identity(rung) for rung in state.rungs}
    duplicate = tuple(rung for rung in correction.rungs if _rung_identity(rung) in existing)
    if duplicate:
        raise ValueError(
            "confirmed correction cannot claim already-owned rung(s): "
            f"{tuple((rung.dest, rung.value) for rung in duplicate)!r}"
        )
    state.rungs = _append_rungs(state.work, list(correction.rungs), state.rungs)
    state.hold_log.append(
        _HoldLogEntry(
            scan=scan,
            source=source,
            rungs=correction.rungs,
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
        existing_ids = {_rung_identity(rung) for rung in checkpoint.world.rungs}
        checkpoint_rungs = [*checkpoint.world.rungs]
        checkpoint_rungs.extend(
            rung for rung in correction.rungs if _rung_identity(rung) not in existing_ids
        )
        banked.append(_checkpoint_with_rungs(checkpoint, checkpoint_rungs, key_config))
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


def _checkpoint_with_rungs(
    checkpoint: _Checkpoint,
    rungs: list[PilotRung],
    key_config: Any,
) -> _Checkpoint:
    """Return one checkpoint re-keyed around an exact executable overlay."""
    if tuple(rungs) == tuple(checkpoint.world.rungs):
        return checkpoint
    work = checkpoint.world.work.fork()
    _set_rungs(work, rungs)
    world = checkpoint.world.set(work=work, rungs=pvector(rungs))
    key = (
        _pilot_world_key(dict(work.state.tags), key_config, rungs)
        if key_config is not None
        else checkpoint.key
    )
    return replace(checkpoint, key=key, world=world)


def _contradicted_corrections(
    state: _PilotState,
    investigation: InvestigationResult,
) -> tuple[_CorrectionReceipt, ...]:
    """Active corrections contradicted by the next incident's exact remedy.

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
    for rung in correction.rungs:
        remedy_rungs.setdefault(rung.dest, []).append((rung.value, rung.operation))

    def _compatible_phases(new_operation: Any, old: PilotRung) -> bool:
        """Opposite values with distinct owner boundaries are temporal phases."""
        return (
            new_operation is not None
            and old.operation is not None
            and _semantic_key(new_operation.until) != _semantic_key(old.operation.until)
        )

    contradicted: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if not receipt.status.effective:
            continue
        admitted: dict[str, list[PilotRung]] = {}
        for rung in receipt.rungs:
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

    Match the exact PILOT write (destination and value), then resolve the last
    active rung for that destination using the same ordered-overlay rule as
    ``_set_rungs``.  This avoids blaming an expired or shadowed correction that
    merely mentions the same tag. Guard ownership is evaluated in the
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

    view = _SnapshotView(dict(witness.owner_snapshot or snapshot), {})
    active_owner: dict[str, PilotRung] = {}
    for rung in state.rungs:
        try:
            active = bool(rung.guard.evaluate(view))
        except (AttributeError, KeyError, TypeError, ValueError):
            active = False
        if active:
            active_owner[rung.dest] = rung

    harmful: list[_CorrectionReceipt] = []
    for receipt in state.correction_receipts:
        if not receipt.status.effective:
            continue
        owns_cause = False
        for rung in receipt.rungs:
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
    revoked_rung_ids = {_rung_identity(rung) for receipt in receipts for rung in receipt.rungs}
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
                rungs=receipt.rungs,
            )
        )

    remaining = [rung for rung in state.rungs if _rung_identity(rung) not in revoked_rung_ids]
    state.rungs = remaining
    _set_rungs(state.work, remaining)
    key_config = state.key_config
    cleaned_checkpoints: list[_Checkpoint] = []
    for saved in state.checkpoints:
        saved_rungs = [
            rung for rung in saved.world.rungs if _rung_identity(rung) not in revoked_rung_ids
        ]
        cleaned_checkpoints.append(_checkpoint_with_rungs(saved, saved_rungs, key_config))
    state.checkpoints = cleaned_checkpoints
    restored_key = state.checkpoints[-1].key
    # The same machine tags now carry different correction knowledge. Permit
    # the retry; the revoked correction identity will be excluded explicitly.
    state.seen_keys.discard(restored_key)
    return tuple(sorted(receipt_ids))


def _investigate_and_revert(
    trial: _TrialResult,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    origin: _RecoveryOrigin,
    retain_if_unresolved: DepartureVerdict | None = None,
) -> tuple[PilotEvent, ...]:
    """Build a bounded incident from ``origin`` through the current world, replay-test
    corrective holds, install the confirmed ones, and revert to the selected
    checkpoint.

    A regression origin anchors at its checkpoint, while a terminal-let-run
    ejection may anchor at the coast start. The origin owns that distinction;
    recovery derives the end from the committed world it is about to revert.
    """
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
    if trial.chase_regression_causes:
        # A watch tag that moved TO a value the target still needs (the
        # checkpoint frontier) is *progress*, not a departure — the coast exists
        # to move it (Heat_CurStep 0->1 en route to 3).  Chasing it spawns
        # corrective holds against the plan itself (lock the enabler of the
        # very advance we wanted).  Only anomalous motion enters the bearing.
        bearing = _deviation_bearing(
            trial,
            frame,
            state.watch_tags,
            trial.bearing_objective.frontier,
        )
        # The incident's evidence is the recorded step timelines inside the
        # window — the trend recorder's pen marks — never a history re-diff.
        # Committed acts are world-side, so reverted operations are already gone
        # and every timeline remains attached to its exact physical step group.
        window_timeline = tuple(
            event
            for act in state.committed_acts
            for event in act.context.timeline
            if origin.anchor_scan <= event.scan <= end_scan
        )
        incident = build_deviation_incident(
            anchor_scan=origin.anchor_scan,
            end_scan=end_scan,
            action=trial.applied,
            bearing=bearing,
            before_snap=origin.before_snap,
            after_snap=trial.fork_snap,
            timeline=window_timeline,
            program=ctx.program,
            channel_tag=trial.zoom_channel_tag,
        )

        # Replay re-arms each step's RECORDED session spec (kind + channel +
        # target off the committed step context), replacing the old positional
        # "last empty-input step is the eject coast" inference.
        replay_steps = tuple(
            _replay_step(step, act.context)
            for act in state.committed_acts
            for step in act.steps
            if step.scan_before >= cp_fork.state.scan_id
        )
        role_tags = coast_departure_tags(state, ctx)
        correction_progress_mark = (
            retain_if_unresolved.progress.source_mark
            if retain_if_unresolved is not None
            and retain_if_unresolved.progress.effect == "preserved"
            else ()
        )
        regression_progress_floor = dict(cp_fork.state.tags)
        regression_progress_floor.update(correction_progress_mark)
        regression_witness = incident_regression_witness(trial.fork, incident)
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
            tuple(state.rungs),
            replay_steps,
            ctx=ctx,
            incident=ReplayIncident(
                channel_tag=trial.zoom_channel_tag,
                channel_target=trial.zoom_target_value,
                terminal_role_tags=(
                    role_tags if trial.motion is MotionKind.COAST_HOLDING_WORLD else None
                ),
                # The replay reproduces the incident, so its eject watch is the
                # departed channel alone when one exists (audit I2 — an explicit
                # caller decision, not buried dispatch); the full role set only
                # when no channel register is recognized.
                watch_roles=(
                    (trial.zoom_channel_tag,) if trial.zoom_channel_tag is not None else role_tags
                ),
                departure_bearing=tuple((d.tag, d.value) for d in incident.departures),
                regression_witness=regression_witness,
                progress_gauge=state.gauge,
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
        needed = list(trial.bearing_objective.frontier)
        investigation = investigate_deviation(
            # Derive hypotheses from the PLC that actually observed the
            # incident.  Replay still starts from ``cp_fork`` above.
            trial.fork,
            incident,
            ctx,
            replay,
            needed=needed,
            installed_rungs=tuple(state.rungs),
            correction_rungs=tuple(
                rung
                for receipt in state.correction_receipts
                if receipt.status.effective
                for rung in receipt.rungs
            ),
            correction_progress_mark=correction_progress_mark,
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
                *_contradicted_corrections(state, investigation),
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
        # The departure earned no gauge credit, but investigation also found no
        # executable correction that preserves the target frontier.  The
        # independently-proven continuation therefore receives the ordinary
        # bounded pending window. If one is already open, retain its
        # original rollback boundary, budget, and the actual first observed
        # landing. The classifier's later quiescent fork is evidence, not
        # permission to skip the next recomputation point.
        assert trial.zoom_channel_tag is not None
        retained = PilotEvent(
            "departure_investigated",
            state.work.state.scan_id,
            {
                "channel_tag": trial.zoom_channel_tag,
                "from_value": trial.before_snap.get(trial.zoom_channel_tag),
                "retained": True,
                "progress": retain_if_unresolved.progress,
                "investigation": investigation_payload,
            },
        )
        if state.pending_departure is not None:
            _bank_pending_landing(trial, state)
            return (retained,)
        return (
            retained,
            *_open_pending_departure(
                retain_if_unresolved,
                trial,
                state,
                ctx,
                trial.zoom_channel_tag,
            ),
        )

    # Legibility (recording only): the channel transition(s) this revert undoes.
    # A destructive move (``S_StateCurrent 6->8`` Aborting) and a program-intended
    # useful program-owned move (``6->11`` Held) both leave the bearing, but only the former
    # is a genuine error — printing the reverted channel edge separates them in
    # every transcript.  Read the channel value at the checkpoint (from) vs. the
    # regressed frame (to); a channel is any opaque-loop pipeline register.
    channel_transitions: tuple[tuple[str, Any, Any], ...] = _channel_transitions(
        ctx, cp_fork, trial.fork_snap
    )

    # Keep the failed action as a nogood in the world where it failed. A
    # replay-confirmed correction creates a different world key, so the same
    # action is naturally eligible there without deleting valid history.
    regression_nogoods = set(investigation_nogoods)
    regression_nogoods.update(trial.regression_nogoods)
    if regression_nogoods:
        ctx.compass, _ = ctx.compass.apply(
            tuple(ActionNogoodObservation(cp_key, ("pair", pair)) for pair in regression_nogoods)
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
        assert confirmed_correction is not None
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
                "from_trend": trial.trend,
                "to_trend": cp_trend,
                "checkpoint_key": cp_key,
                "regression_nogoods": frozenset(regression_nogoods),
                "rungs": tuple(state.rungs),
                "channel_transitions": channel_transitions,
                "investigation": investigation_payload,
                "revoked_corrections": revoked_ids,
                "revoked_rungs": tuple(
                    rung for receipt in revoked_receipts for rung in receipt.rungs
                ),
            },
        ),
    )
