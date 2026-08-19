"""Judge an executed fork before it may replace the current world.

``verify_gates`` applies avoid and target checks, rejects spins and structural
dead ends, delegates motion attribution and progress classification to
``outcome.py``, then decides whether that classified landing may revisit an
executable world. It reports a suspicious excursion to the drive loop without
performing runtime investigation. ``verify_excursion_replay`` judges the one
replay returned by that owner and continues the remaining gates after spin.

Passing these gates makes a trial eligible for commit and progress monitoring;
it does not guarantee that later assessment will retain the committed world.
Gate diagnostics are recorded as ``PilotGateEvent`` values on the attempt.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

import pyrung.core.analysis.pilot.trial_gates as _trial_gates
from pyrung.core.analysis.pilot.attempt_observation import (
    _observe_intrascan_act_occurrence,
    _observe_investigation_producer,
    _observe_temporal_setup_occurrences,
    _route_blocker_crossings,
)
from pyrung.core.analysis.pilot.avoid import _avoid_snap_names
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWorkMovement,
    EarnedWorkReceipt,
    earned_work_is_useful_motion,
)
from pyrung.core.analysis.pilot.effect_observation import (
    effect_reached_consumer,
    fulfilled_expectation_observations,
    observe_execution_window,
)
from pyrung.core.analysis.pilot.effects import (
    promote_route_landing_observations,
)
from pyrung.core.analysis.pilot.execution import (
    ChannelMotion,
    IntrascanActReceipt,
    InvestigationProducerReceipt,
    MotionKind,
    PulseHorizon,
    ScanProgressReceipt,
    capture_execution_spans,
)
from pyrung.core.analysis.pilot.navigation_contracts import (
    ActSource,
    Coast,
    Dwell,
    IntrascanPulse,
    LocalProgressKind,
    ObserveScan,
    ProgramScan,
    _ActionPair,
    act_identity,
)
from pyrung.core.analysis.pilot.outcome import (
    Agency,
    BearingEffect,
    ProgressEffect,
    TrialAssessment,
    assess_outcome,
)
from pyrung.core.analysis.pilot.requirement_admission import active_requirement_violations
from pyrung.core.analysis.pilot.trace import (
    target_reached,
    trace_back,
)
from pyrung.core.analysis.pilot.trace_read import TraceReadConstraints
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    PilotGateEvent,
    RevisitCredential,
    TargetReached,
    _AcceptedTrial,
    _AttemptResult,
    _ExecutedAttempt,
    _PulseState,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _semantic_key
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.instruction.advance import constraint_holds

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.investigation_replay import ExcursionResult


def _rebind_replay_attempt(
    attempt: _ExecutedAttempt,
    replay_trial: _PulseState,
) -> _ExecutedAttempt:
    """Replace a replayed fork and freeze its own physical execution receipt."""

    execution = attempt.execution
    if execution is None:
        raise ValueError("excursion replay requires the opening execution receipt")

    immediate = observe_execution_window(
        attempt.bearing.expectation,
        replay_trial.fork,
        scan_before=replay_trial.scan_before,
        action_scan=(
            None
            if isinstance(attempt.bearing.act, (Coast, Dwell, ObserveScan, ProgramScan))
            else replay_trial.action_scan
        ),
        coast_receipt=replay_trial.coast_receipt,
        kernel_scan_ids=replay_trial.kernel_scan_ids,
        projection_at=replay_trial.projection_at,
    )
    landing = observe_execution_window(
        attempt.landing_expectation,
        replay_trial.fork,
        scan_before=replay_trial.scan_before,
        action_scan=(
            None
            if isinstance(attempt.bearing.act, (Coast, Dwell, ObserveScan, ProgramScan))
            else replay_trial.action_scan
        ),
        coast_receipt=replay_trial.coast_receipt,
        kernel_scan_ids=replay_trial.kernel_scan_ids,
        projection_at=replay_trial.projection_at,
    )
    if landing:
        projections = tuple(
            projection
            for scan_id in replay_trial.kernel_scan_ids
            if replay_trial.scan_before < scan_id <= replay_trial.fork.state.scan_id
            and (projection := replay_trial.projection_at(scan_id)) is not None
        )
        landing = promote_route_landing_observations(
            landing,
            projections,
            final_landing=replay_trial.snap,
        )
    observations = (*immediate, *landing)
    source_snap = replay_trial.source_snap
    configurations = tuple(replay_trial.applied_configurations)
    replay_execution = replace(
        execution,
        before_snap=(source_snap or execution.before_snap),
        after_snap=replay_trial.snap,
        channel_motion=replay_trial.channel_motion,
        coast_receipt=replay_trial.coast_receipt,
        timeline=replay_trial.timeline,
        effect_observations=tuple(
            observation.diagnostic_snapshot() for observation in observations
        ),
        replay_motion=replay_trial.replay_motion,
        spans=capture_execution_spans(
            replay_trial.fork,
            replay_trial.kernel_scan_ids,
        ),
        source_scan=replay_trial.scan_before,
        applied_configurations=configurations,
        stop=replay_trial.stop_receipt,
    )
    return replace(
        attempt,
        pulse=replay_trial,
        effect_observations=observations,
        execution=replay_execution,
    )


def _owned_channel_motion(
    trial: _PulseState,
    motion: ChannelMotion,
) -> ChannelMotion:
    """Interpret a raw coast receipt against verification's selected owner.

    An inner advance seek arms the outer route channel as its departure trigger.
    When that outer channel lands exactly on its requested value, the raw inner
    receipt necessarily says ``departed`` even though the selected outer
    operation was reached.  Rebase that one observation here; relational inner
    boundaries retain their own ``reached`` receipt even when their scalar
    heading was crossed rather than equalled.
    """
    if not motion.active:
        return motion
    channel_tag = motion.channel_tag
    assert channel_tag is not None
    if _values_match(trial.snap.get(channel_tag), motion.target_value):
        return replace(motion, stop_reason="reached")
    receipt = trial.coast_receipt
    if receipt is not None:
        return replace(motion, stop_reason=receipt.stop_reason)
    stop_reason = (
        "departed"
        if not _values_match(trial.snap.get(channel_tag), trial.action_snap.get(channel_tag))
        else "timeout"
    )
    return replace(motion, stop_reason=stop_reason)


def _replayed_channel_motion(
    replay_snap: dict[str, Any],
    source_snap: dict[str, Any],
    motion: ChannelMotion,
) -> ChannelMotion:
    """Classify a correction replay without reusing the original coast receipt."""
    if not motion.active:
        return motion
    channel_tag = motion.channel_tag
    assert channel_tag is not None
    if motion.boundary is not None and constraint_holds(motion.boundary, replay_snap) is True:
        return replace(motion, stop_reason="reached")
    if _values_match(replay_snap.get(channel_tag), motion.target_value):
        return replace(motion, stop_reason="reached")
    if not _values_match(replay_snap.get(channel_tag), source_snap.get(channel_tag)):
        return replace(motion, stop_reason="departed")
    return replace(motion, stop_reason="timeout")


def _executed_source_world_key(frame: Any, state: Any) -> tuple[Any, ...]:
    """Source identity after execution installed bearing prerequisites."""
    key_config = state.key_config
    if key_config is None:
        return frame.key
    return _pilot_world_key(
        frame.snap,
        key_config,
        state.pilot_rungs,
        getattr(state, "active_requirements", ()),
    )


def _selected_route_landing_tree(
    trial: _PulseState,
    frame: Any,
    ctx: Any,
) -> Any | None:
    """Read the landing against the same chart route that selected the act."""

    try:
        tree = trace_back(
            ctx.target.tag,
            ctx.target.value,
            trial.snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            constraints=TraceReadConstraints(
                clear_only=ctx.clear_only,
                opaque_loop=ctx.opaque_loop,
                pipeline_internal_tags=ctx.pipeline_internal_tags,
                route=ctx.route,
                prior=ctx.domain_prior,
                avoid_pred=ctx.avoid_pred,
            ),
        )
    except Exception:  # noqa: BLE001 - unavailable route evidence fails closed
        return None
    return tree


def _accepted_trial(
    attempt: _ExecutedAttempt,
    frame: Any,
    gate_events: list[PilotGateEvent],
    channel_motion: ChannelMotion,
    earned_work_receipt: EarnedWorkReceipt,
    verification: TargetReached | AssessedMotion,
    *,
    exact_frontier_advanced: bool = False,
    investigation_producer: InvestigationProducerReceipt | None = None,
    intrascan_act: IntrascanActReceipt | None = None,
) -> _AcceptedTrial:
    """Preserve the final executed attempt and its PLC-free evidence."""
    pulse = attempt.pulse
    immediate_expectation = attempt.bearing.expectation
    immediate_obligations = (
        {id(obligation) for obligation in immediate_expectation.obligations}
        if immediate_expectation is not None
        else set()
    )
    landing_expectation = attempt.landing_expectation
    landing_obligations = (
        {id(obligation) for obligation in landing_expectation.obligations}
        if landing_expectation is not None
        else set()
    )
    survived = tuple(
        observation
        for observation in attempt.effect_observations
        if id(observation.obligation) in immediate_obligations
        and effect_reached_consumer(observation)
        and observation.appeared is not None
    )
    route_survived = tuple(
        observation
        for observation in attempt.effect_observations
        if id(observation.obligation) in landing_obligations
        and effect_reached_consumer(observation)
        and observation.appeared is not None
    )
    selected_receipts = (*survived, *route_survived)
    selected_producer_landing = any(
        _values_match(
            pulse.snap.get(observation.obligation.tag),
            observation.obligation.value,
        )
        or any(
            node.tag == observation.obligation.tag
            and not node.satisfied
            and _values_match(pulse.snap.get(node.tag), node.value)
            for node in frame.tree.iter_nodes()
        )
        for observation in selected_receipts
    )
    if isinstance(verification, TargetReached):
        progress_kind = "target"
    elif earned_work_is_useful_motion(earned_work_receipt):
        progress_kind = "earned-work"
    elif selected_receipts:
        # A consumed selected-producer occurrence is stronger than the
        # candidate's generic TRACE_SETUP declaration.  Its value may be
        # overwritten later in the same owned window by useful program motion;
        # VERIFY has already rejected the harmful overwrite case.  Preserve
        # the accepted landing as the new route tip instead of asking legacy
        # global trace distance to judge two unrelated route coordinates.
        progress_kind = "selected-producer"
    elif (
        exact_frontier_advanced or verification.assessment.progress is ProgressEffect.FORWARD
    ) and (
        not (isinstance(attempt.bearing.act, Coast) and attempt.bearing.act.mode == "bearing")
        or not channel_motion.active
        or channel_motion.reached
    ):
        # A bearing coast owns its requested channel heading before it owns a
        # generic target frontier. If that channel ejected somewhere else,
        # conditions made true earlier in the coast are not evidence that its
        # retained tip advanced the selected operation. A terminal coast is
        # deliberately waiting for program-owned ejection, while a pulse's S1
        # may be productive even when its optional S2 look-ahead departs.
        # Leave only a wrong bearing-coast landing to post-commit departure
        # handling, which can inspect and replay the incident from its source.
        progress_kind = "frontier"
    elif attempt.bearing.act.policy.local_progress is LocalProgressKind.TRACE_SETUP:
        # A stable setup without a stronger execution receipt remains
        # provisional. Its first landing still needs ordinary post-commit
        # look-ahead before the setup is banked.
        progress_kind = "conductivity"
    elif attempt.bearing.act.policy.local_progress is not None:
        progress_kind = "conductivity"
    else:
        progress_kind = None
    productive_scan = (
        min(
            observation.appeared.scan_id
            for observation in selected_receipts
            if observation.appeared is not None
        )
        if selected_receipts
        else pulse.action_scan
        if not isinstance(attempt.bearing.act, (Coast, Dwell, ObserveScan, ProgramScan))
        and pulse.action_scan is not None
        else pulse.fork.state.scan_id
    )
    scan_progress = (
        ScanProgressReceipt(
            source_scan=pulse.scan_before,
            productive_scan=productive_scan,
            landing_scan=pulse.fork.state.scan_id,
            kind=progress_kind,
            selected_act=act_identity(attempt.bearing.act),
            distance_after=(verification.trend if isinstance(verification, AssessedMotion) else 0),
            landing_owns_tip=(
                selected_producer_landing if progress_kind == "selected-producer" else True
            ),
        )
        if progress_kind is not None
        else None
    )
    if attempt.execution is None:
        raise ValueError("VERIFY requires an immutable pre-verification execution receipt")
    execution = replace(
        attempt.execution,
        before_snap=frame.snap,
        channel_motion=channel_motion,
        scan_progress=scan_progress,
        investigation_producer=investigation_producer,
        intrascan_act=intrascan_act,
    )
    return _AcceptedTrial(
        attempt=replace(attempt, execution=execution),
        earned_work_receipt=earned_work_receipt,
        gate_events=tuple(gate_events),
        verification=verification,
    )


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Verify pipeline — the shared gate sequence
# ---------------------------------------------------------------------------


def _verify_after_spin(
    attempt: _ExecutedAttempt,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
    avoid_names: list[str],
    earned_work_receipt: EarnedWorkReceipt,
    channel_motion: ChannelMotion,
    source_world_key: tuple[Any, ...],
    observations: tuple[Any, ...] = (),
) -> _AttemptResult:
    """Run the gates after spin judgment for an original or replayed attempt."""
    trial = attempt.pulse
    bearing = attempt.bearing
    policy = bearing.act.policy
    nogood_pair = policy.nogood_pair

    def _reject() -> _AttemptResult:
        return _AttemptResult(
            trial=None,
            executed=attempt,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            observations=observations,
            avoid_names=tuple(avoid_names),
        )

    if target_reached(
        trial.snap,
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    ):
        gate_events.append(PilotGateEvent("target", f"{ctx.target.tag}={ctx.target.value!r}"))
        accepted = _accepted_trial(
            attempt,
            frame,
            gate_events,
            channel_motion,
            earned_work_receipt,
            TargetReached(),
        )
        return _AttemptResult(
            trial=accepted,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            observations=observations,
            avoid_names=tuple(avoid_names),
        )

    dead_end = _trial_gates._gate_dead_end(
        trial,
        policy.applied,
        frame,
        state,
        ctx,
        target=bearing.objective.target,
        earned_work_receipt=earned_work_receipt,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        channel_motion=channel_motion,
        accept_temporal_progress=(policy.local_progress is LocalProgressKind.TEMPORAL_EDGE),
    )
    if dead_end is None:
        return _reject()

    assessment = assess_outcome(
        trial,
        policy.applied,
        frame,
        ctx,
        dead_end.trend,
        dead_end.has_new_frontier,
        route_prescribed=policy.route_prescribed,
        channel_motion=channel_motion,
        earned_work_receipt=earned_work_receipt,
    )

    if not assessment.accepted:
        if nogood_pair is not None:
            collected_nogoods.append(nogood_pair)
        _trial_gates._record_gate(
            "BEARING-COAST-STALL" if channel_motion.active else "BAD-EDGE",
            f": distance {frame.distance_before} -> {dead_end.trend}",
            gate_events,
            evidence={
                "accepted": assessment.accepted,
                "agency": assessment.agency.value,
                "bearing": assessment.bearing.value,
                "progress": assessment.progress.value,
                "new_frontier": assessment.new_frontier,
                "trend_before": frame.distance_before,
                "trend_after": dead_end.trend,
                "bearing_coast_channel_tag": channel_motion.channel_tag,
                "bearing_coast_target_value": channel_motion.target_value,
                "bearing_coast_actual_value": (
                    trial.snap.get(channel_motion.channel_tag)
                    if channel_motion.channel_tag is not None
                    else None
                ),
            },
        )
        return _reject()

    departure_credential = (
        RevisitCredential(
            kind="departure",
            source_world=source_world_key,
            act=act_identity(bearing.act),
            transition=(
                channel_motion.channel_tag,
                _semantic_key(frame.snap.get(channel_motion.channel_tag)),
                _semantic_key(channel_motion.target_value),
                _semantic_key(trial.snap.get(channel_motion.channel_tag)),
            ),
        )
        if channel_motion.departed and channel_motion.channel_tag is not None
        else None
    )
    earned_credential = (
        RevisitCredential(
            kind="earned-work",
            source_world=source_world_key,
            act=act_identity(bearing.act),
            transition=(
                _semantic_key(earned_work_receipt.source_mark),
                _semantic_key(earned_work_receipt.landing_mark),
            ),
        )
        if earned_work_is_useful_motion(earned_work_receipt)
        else None
    )
    if not _trial_gates._gate_revisit(
        trial,
        state,
        earned_work_receipt=earned_work_receipt,
        earned_credential=earned_credential,
        departure_credential=departure_credential,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
    ):
        return _reject()

    gate_events.append(
        PilotGateEvent(
            assessment.bearing.value,
            f"distance {frame.distance_before} -> {dead_end.trend}",
            evidence={
                "accepted": assessment.accepted,
                "agency": assessment.agency.value,
                "bearing": assessment.bearing.value,
                "progress": assessment.progress.value,
                "new_frontier": assessment.new_frontier,
            },
        )
    )

    return _AttemptResult(
        trial=_accepted_trial(
            attempt,
            frame,
            gate_events,
            channel_motion,
            earned_work_receipt,
            AssessedMotion(
                new_key=trial.key,
                trend=dead_end.trend,
                assessment=assessment,
                revisit_credentials=tuple(
                    credential
                    for credential in (departure_credential, earned_credential)
                    if credential is not None
                ),
            ),
            exact_frontier_advanced=bool(getattr(dead_end, "advanced_frontier", ())),
        ),
        gate_events=tuple(gate_events),
        nogood_pairs=frozenset(collected_nogoods),
        confirmed_correction=trial.confirmed_correction,
        observations=observations,
        avoid_names=tuple(avoid_names),
    )


def verify_excursion_replay(
    detected_result: _AttemptResult,
    investigation_result: ExcursionResult,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _AttemptResult:
    """Judge one drive-loop-owned excursion replay after the spin gate.

    The original attempt's observations and partial gate history survive
    unchanged.  The replay is checked against every retained history snapshot
    for ``avoid=`` before its exact correction, timeline, key, and earned-work
    receipt may proceed through the remaining gates.
    """
    attempt = detected_result.excursion_attempt
    if attempt is None:
        raise ValueError("excursion replay requires the detected executed attempt")

    trial = attempt.pulse
    policy = attempt.bearing.act.policy
    nogood_pair = policy.nogood_pair
    gate_events = list(detected_result.gate_events)
    collected_nogoods = list(detected_result.nogood_pairs)
    avoid_names = list(detected_result.avoid_names)
    observations = detected_result.observations

    if investigation_result.replay_fork is None or investigation_result.correction is None:
        _trial_gates._record_gate(
            "EXCURSION-NO-HOLDS" if investigation_result.reverted else "EXCURSION-REPLAY-FAIL",
            gate_events=gate_events,
        )
        return _AttemptResult(
            trial=None,
            executed=attempt,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=detected_result.confirmed_correction,
            observations=observations,
            avoid_names=tuple(avoid_names),
        )

    key_config = state.key_config
    assert key_config is not None
    replay_fork = investigation_result.replay_fork
    correction = investigation_result.correction
    replay_snap = dict(replay_fork.state.tags)
    replay_pilot_rungs = (*state.pilot_rungs, *correction.pilot_rungs)

    if ctx.avoid_pred is not None:
        replay_violations: list[str] = list(_avoid_snap_names(ctx.avoid_pred, replay_snap))
        for scan in range(trial.scan_before + 1, replay_fork.state.scan_id + 1):
            replay_violations.extend(
                _trial_gates._avoid_names_after_clear(
                    ctx.avoid_pred,
                    frame.snap,
                    dict(replay_fork.history.at(scan).tags),
                )
            )
        if replay_violations:
            names = tuple(dict.fromkeys(replay_violations))
            avoid_names.extend(names)
            if nogood_pair is not None:
                collected_nogoods.append(nogood_pair)
            _trial_gates._record_gate(
                "AVOID",
                f": excursion replay enters avoid: {', '.join(names)}",
                gate_events,
            )
            return _AttemptResult(
                trial=None,
                executed=attempt,
                gate_events=tuple(gate_events),
                nogood_pairs=frozenset(collected_nogoods),
                confirmed_correction=detected_result.confirmed_correction,
                observations=observations,
                avoid_names=tuple(avoid_names),
            )

    replay_key = _pilot_world_key(
        replay_snap,
        key_config,
        replay_pilot_rungs,
        getattr(state, "active_requirements", ()),
    )
    _trial_gates._record_gate(
        "EXCURSION-REPLAY-OK",
        (
            f": reverted={investigation_result.reverted}, "
            f"pilot_rungs={tuple((r.dest, r.value) for r in correction.pilot_rungs)}"
        ),
        gate_events,
    )
    replay_trial = replace(
        trial,
        fork=replay_fork,
        snap=replay_snap,
        key=replay_key,
        coast_receipt=None,
        timeline=investigation_result.replay_timeline,
        kernel_scan_ids=investigation_result.replay_kernel_scan_ids,
        confirmed_correction=correction,
    )
    earned_work = getattr(state, "earned_work", None)
    earned_work_receipt = (
        earned_work.receipt(frame.snap, replay_snap)
        if earned_work is not None
        else EarnedWorkReceipt()
    )
    declared_motion = (
        ChannelMotion(
            policy.heading.channel_tag,
            policy.heading.target_value,
            policy.heading.boundary,
        )
        if policy.heading is not None
        else ChannelMotion()
    )
    channel_motion = _replayed_channel_motion(
        replay_snap,
        frame.snap,
        trial.channel_motion if trial.channel_motion.active else declared_motion,
    )
    replay_trial = replace(replay_trial, channel_motion=channel_motion)
    replay_attempt = _rebind_replay_attempt(attempt, replay_trial)
    return _verify_after_spin(
        replay_attempt,
        frame,
        state,
        ctx,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        avoid_names=avoid_names,
        earned_work_receipt=earned_work_receipt,
        channel_motion=channel_motion,
        source_world_key=_pilot_world_key(
            frame.snap,
            key_config,
            replay_pilot_rungs,
            getattr(state, "active_requirements", ()),
        ),
        observations=observations,
    )


def _verify_gates(
    attempt: _ExecutedAttempt,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _AttemptResult:
    """Apply the shared trial gates to an executed pulse or coast.

    Runs avoid and target checks, then spin and dead-end gates, outcome
    classification, and finally revisit admission. Condition-like avoids fired by a folded coast
    arrive on its receipt; opaque callables are checked only at endpoints and
    real snapshots retained by execution. All steering execution modes
    converge here.

    Verification owns the accepted trial's earned-work receipt. An excursion
    returns its exact attempt to the drive loop; the replay judge computes the
    replacement receipt before continuing the remaining gates after spin.
    """
    trial = attempt.pulse
    bearing = attempt.bearing
    policy = bearing.act.policy
    applied_actions = policy.applied
    nogood_pair = policy.nogood_pair
    declared_motion = (
        ChannelMotion(
            policy.heading.channel_tag,
            policy.heading.target_value,
            policy.heading.boundary,
        )
        if policy.heading is not None
        else ChannelMotion()
    )
    channel_motion = _owned_channel_motion(
        trial,
        trial.channel_motion if trial.channel_motion.active else declared_motion,
    )
    gate_events: list[PilotGateEvent] = []
    collected_nogoods: list[_ActionPair] = []
    avoid_violations: list[str] = []
    earned_work = getattr(state, "earned_work", None)
    earned_work_receipt = (
        earned_work.receipt(frame.snap, trial.snap)
        if earned_work is not None
        else EarnedWorkReceipt()
    )

    def _reject(
        *,
        nogoods: Any = None,
        avoid_names: Any = None,
        proof_rejection: bool = False,
    ) -> _AttemptResult:
        return _AttemptResult(
            trial=None,
            executed=attempt,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods if nogoods is None else nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(avoid_violations if avoid_names is None else avoid_names),
            proof_rejection=proof_rejection,
        )

    def _accept_target() -> _AttemptResult:
        gate_events.append(PilotGateEvent("target", f"{ctx.target.tag}={ctx.target.value!r}"))
        return _AttemptResult(
            trial=_accepted_trial(
                attempt,
                frame,
                gate_events,
                channel_motion,
                earned_work_receipt,
                TargetReached(),
            ),
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(avoid_violations),
        )

    # ── Scan gate (avoid=) ────────────────────────────────────────────────
    # Settled state first (the original veto: never rest in the avoided region).
    # Then transient coverage: retained pulse/settle snapshots plus the coast
    # owner's exact avoid-firing receipt. Condition metadata makes skipped
    # logical spans observable; opaque callables cover only real observations.
    # Both arms nogood the choice and record the violated names for the terminal
    # decline.
    if ctx.avoid_pred is not None:
        coast_avoided = (
            tuple(trial.coast_receipt.avoided) if trial.coast_receipt is not None else ()
        )
        if coast_avoided:
            gate_events.append(
                PilotGateEvent(
                    "avoid",
                    f"coast enters avoid: {', '.join(coast_avoided)}",
                )
            )
            return _reject(
                nogoods=({nogood_pair} if nogood_pair is not None else ()),
                avoid_names=coast_avoided,
            )
        settled = _avoid_snap_names(ctx.avoid_pred, trial.snap)
        if settled:
            gate_events.append(
                PilotGateEvent("avoid", f"settled state matches avoid: {', '.join(settled)}")
            )
            return _reject(
                nogoods=({nogood_pair} if nogood_pair is not None else ()),
                avoid_names=settled,
            )
        for snap in (trial.action_snap, *trial.wait_snaps, trial.post_pulse_snap):
            wink = _trial_gates._avoid_names_after_clear(ctx.avoid_pred, frame.snap, snap)
            if wink:
                gate_events.append(
                    PilotGateEvent("avoid", f"transient scan enters avoid: {', '.join(wink)}")
                )
                return _reject(
                    nogoods=({nogood_pair} if nogood_pair is not None else ()),
                    avoid_names=wink,
                )

    if (
        policy.pulse_horizon is PulseHorizon.CONSUMER_BOUNDARY
        and (trial.stop_receipt is None or trial.stop_receipt.consumer_boundary_reached is not True)
        and not target_reached(
            trial.snap,
            ctx.target.tag,
            ctx.target.value,
            ctx.target.predicate,
        )
    ):
        gate_events.append(
            PilotGateEvent(
                "consumer-execution-horizon-rejected",
                "transaction did not evaluate its exact consumer occurrence",
            )
        )
        return _reject(nogoods=(), proof_rejection=True)

    # An intervention may explore a new frontier, but it may not erase
    # target-relative work the current world has already earned.  Earned work is
    # deliberately conservative: absent or unclassifiable coordinates yield
    # ``unknown``, never a guessed veto.  Coasts are excluded here because a
    # backward move during a coast is program motion owned by post-commit
    # investigation/recovery, not a destructive operator choice.
    if (
        applied_actions
        and policy.motion is MotionKind.INTERVENTION
        and earned_work_receipt.movement is EarnedWorkMovement.BACKWARD
    ):
        gate_events.append(
            PilotGateEvent(
                "banked-work",
                "intervention would erase target-relative earned work",
                evidence={
                    "source_mark": earned_work_receipt.source_mark,
                    "landing_mark": earned_work_receipt.landing_mark,
                    "effect": EarnedWorkMovement.BACKWARD.value,
                },
            )
        )
        return _reject(
            nogoods=({nogood_pair} if nogood_pair is not None else policy.regression_nogoods)
        )

    # Exact selected-effect evidence is stronger than an endpoint coincidence.
    # A proved failed obligation may derive a narrower requirement, but it is
    # never accepted merely because another write also reached the target and
    # is never converted into an empirical action nogood. UNKNOWN remains an
    # ambiguous observation and continues through ordinary verification.
    effect_violations = _trial_gates._proved_effect_violations(attempt)
    if effect_violations:
        gate_events.append(
            PilotGateEvent(
                "expectation-violated",
                "selected effect failed before its obliged consumer",
                evidence={
                    "dispositions": tuple(
                        observation.disposition for observation in effect_violations
                    )
                },
            )
        )
        return _reject(nogoods=(), proof_rejection=True)

    # Requirements without an executable overlay (notably configured and
    # program-written operands) are still real navigation constraints. Verify
    # the accepted fork did not turn a proved source truth false before target
    # or generic landing acceptance.
    requirement_violations = active_requirement_violations(
        tuple(getattr(state, "active_requirements", ())),
        dict(frame.snap),
        dict(trial.snap),
    )
    if requirement_violations:
        gate_events.append(
            PilotGateEvent(
                "requirement-violated",
                "candidate invalidated an active requirement",
                evidence={
                    "requirements": tuple(
                        getattr(
                            requirement,
                            "navigation_identity",
                            getattr(requirement, "condition", None),
                        )
                        for requirement in requirement_violations
                    )
                },
            )
        )
        return _reject(nogoods=(), proof_rejection=True)

    intrascan_act_receipt = None
    if isinstance(bearing.act, (ProgramScan, IntrascanPulse)):
        expected = bearing.act.expected_write
        stage_receipt = _observe_intrascan_act_occurrence(attempt)
        if not stage_receipt.witnessed:
            gate_events.append(
                PilotGateEvent(
                    "intrascan-stage-rejected",
                    "the exact producer occurrence was not observed and retained",
                    {
                        "projection_available": stage_receipt.projection_available,
                        "matching_writes": stage_receipt.matching_writes,
                        "retained": stage_receipt.retained,
                        "expected_write": expected,
                    },
                )
            )
            return _reject(nogoods=(), proof_rejection=True)
        intrascan_act_receipt = stage_receipt.receipt

    # Reaching the target does not pardon an intervention that got there by
    # erasing already-earned work (for example, calling init so a completion
    # bit momentarily reads true).  The banked-work veto therefore precedes
    # target acceptance.
    if target_reached(
        trial.snap,
        ctx.target.tag,
        ctx.target.value,
        ctx.target.predicate,
    ):
        return _accept_target()

    if isinstance(bearing.act, (ObserveScan, ProgramScan, IntrascanPulse)):
        exact_one_scan = (
            trial.fork.state.scan_id == trial.scan_before + 1
            and trial.kernel_scan_ids == (trial.scan_before + 1,)
        )
        if not exact_one_scan:
            gate_events.append(
                PilotGateEvent(
                    "single-program-scan-rejected",
                    "program scan did not execute exactly once",
                )
            )
            return _reject(nogoods=(), proof_rejection=True)
        if isinstance(bearing.act, ObserveScan):
            gate_events.append(
                PilotGateEvent(
                    "entry-observed",
                    "one exact program scan is available for landing orientation",
                )
            )
        elif isinstance(bearing.act, ProgramScan):
            gate_events.append(
                PilotGateEvent(
                    "intrascan-stage-observed",
                    "one exact producer scan is available for fresh Compass orientation",
                    {"expected_write": bearing.act.expected_write},
                )
            )
        else:
            gate_events.append(
                PilotGateEvent(
                    "intrascan-consumer-observed",
                    "one exact scan-start steer reached its conducted consumer",
                    {"expected_write": bearing.act.expected_write},
                )
            )
        assessment = TrialAssessment(
            agency=(Agency.PILOT if isinstance(bearing.act, IntrascanPulse) else Agency.PROGRAM),
            bearing=BearingEffect.SATISFIED,
            progress=ProgressEffect.UNCHANGED,
            new_frontier=False,
            accepted=True,
        )
        accepted = _accepted_trial(
            attempt,
            frame,
            gate_events,
            channel_motion,
            earned_work_receipt,
            AssessedMotion(
                new_key=trial.key,
                trend=frame.distance_before,
                assessment=assessment,
            ),
            intrascan_act=intrascan_act_receipt,
        )
        scan_progress = accepted.execution.scan_progress
        assert scan_progress is not None
        accepted = replace(
            accepted,
            attempt=replace(
                accepted.attempt,
                execution=replace(
                    accepted.execution,
                    scan_progress=replace(
                        scan_progress,
                        kind=(
                            "observation"
                            if isinstance(bearing.act, ObserveScan)
                            else "intrascan-direct"
                            if isinstance(bearing.act, IntrascanPulse)
                            else "intrascan-stage"
                        ),
                        landing_owns_tip=not isinstance(bearing.act, ObserveScan),
                    ),
                ),
            ),
        )
        return _AttemptResult(
            trial=accepted,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(),
        )

    execution_configurations = (
        attempt.execution.applied_configurations
        if attempt.execution is not None
        else tuple(getattr(trial, "applied_configurations", ()))
    )
    configured_actions = tuple(
        assignment
        for configuration in execution_configurations
        for assignment in configuration.assignments
    )
    configured_temporal_edge = bool(
        policy.local_progress is LocalProgressKind.TEMPORAL_EDGE and configured_actions
    )
    if (
        policy.local_progress
        in {
            LocalProgressKind.TRACE_SETUP,
            LocalProgressKind.REARM,
            LocalProgressKind.TEMPORAL_SETUP,
            LocalProgressKind.THEORY_CORRECTIVE,
        }
        or configured_temporal_edge
    ):
        orientation = bearing.orientation
        trace_details = (
            orientation.candidates.trace.detail_by_pair if orientation is not None else {}
        )
        primary = policy.primary_action
        primary_detail = trace_details.get(primary) if primary is not None else None
        primary_operation = getattr(primary_detail, "operation", None)
        declared_lifetime = getattr(primary_detail, "until", None)
        if declared_lifetime is None:
            declared_lifetime = getattr(primary_operation, "until", None)
        selected_effect_consumed = any(
            observation.appeared is not None and effect_reached_consumer(observation)
            for observation in attempt.effect_observations
        )
        investigation_producer_receipt = _observe_investigation_producer(attempt)
        investigation_producer_witnessed = investigation_producer_receipt.witnessed
        selected_trace_setup = bool(
            policy.source is ActSource.TRACE
            and primary is not None
            and primary_detail is not None
            and primary_detail.pair == primary
        )
        trace_setup_owned = (
            policy.local_progress is not LocalProgressKind.TRACE_SETUP
            or selected_trace_setup
            or declared_lifetime is not None
            or selected_effect_consumed
            or investigation_producer_witnessed
        )
        landing_tree = _selected_route_landing_tree(trial, frame, ctx)
        landing_distance = landing_tree.unsatisfied_count() if landing_tree is not None else None
        route_blockers = (
            _route_blocker_crossings(
                attempt,
                frame,
                ctx,
                pilot_rungs=state.pilot_rungs,
                resting=ctx.resting,
            )
            if policy.local_progress is LocalProgressKind.TRACE_SETUP
            else ()
        )
        route_owned = policy.local_progress is not LocalProgressKind.TRACE_SETUP or (
            investigation_producer_witnessed
            or (
                landing_distance is not None
                and landing_distance <= frame.distance_before
                and not route_blockers
            )
        )
        changed = tuple(
            (tag, value)
            for tag, value in applied_actions
            if not _values_match(frame.snap.get(tag), value)
            and _values_match(trial.snap.get(tag), value)
        )
        assignments_reached = bool(applied_actions) and all(
            _values_match(trial.snap.get(tag), value) for tag, value in applied_actions
        )
        progress_requirements = (
            policy.local_progress_requirements
            if policy.local_progress_requirements
            else getattr(ctx, "temporal_requirements", ())
        )
        requirement_tags = frozenset(
            tag
            for requirement in progress_requirements
            if (tag := getattr(getattr(requirement, "condition", None), "tag", None)) is not None
        )
        temporal_setup_consumed: tuple[_ActionPair, ...] = ()
        temporal_setup_requirements_observed = False
        temporal_setup_observation_receipts: tuple[Any, ...] = ()
        temporal_setup_actions = (
            tuple(pair for pair in configured_actions if pair[0] in requirement_tags)
            if configured_temporal_edge
            else tuple(applied_actions)
        )
        if (
            (policy.local_progress is LocalProgressKind.TEMPORAL_SETUP or configured_temporal_edge)
            and temporal_setup_actions
            and progress_requirements
        ):
            temporal_receipt = _observe_temporal_setup_occurrences(
                attempt,
                tuple(progress_requirements),
                temporal_setup_actions,
                ctx,
            )
            temporal_setup_consumed = temporal_receipt.consumed_actions
            temporal_setup_requirements_observed = temporal_receipt.requirements_observed
            temporal_setup_observation_receipts = temporal_receipt.observations
        route_advanced = bool(
            landing_distance is not None
            and landing_distance < frame.distance_before
            and not route_blockers
        )
        # Some externally writable configuration registers are intentionally
        # consumed and reset by the program in the assertion scan.  They are
        # still real intrascan steering when the exact disposable trial moves
        # the selected route closer.  Requiring their patched value to survive
        # would reject the successful transaction merely because its consumer
        # acknowledged it.
        intrascan_configuration_consumed = bool(
            policy.local_progress is LocalProgressKind.TRACE_SETUP
            and route_advanced
            and any(
                ctx.pdg.writers_of.get(tag, frozenset())
                and not _values_match(frame.snap.get(tag), value)
                and not _values_match(trial.snap.get(tag), value)
                for tag, value in applied_actions
            )
        )
        requirements_reached = all(
            constraint_holds(cast(Any, requirement.condition), trial.snap) is True
            for requirement in progress_requirements
        )
        expectation_boundaries_preserved = all(
            obligation.boundary is None
            or _values_match(
                trial.snap.get(obligation.boundary[0]),
                obligation.boundary[1],
            )
            for obligation in (
                bearing.expectation.obligations if bearing.expectation is not None else ()
            )
        )
        declared_boundary_preserved = not (
            policy.local_progress is LocalProgressKind.TRACE_SETUP
            and (channel_motion.departed or not expectation_boundaries_preserved)
        )
        local_progress_reached = (
            (
                (bool(changed) and assignments_reached)
                or intrascan_configuration_consumed
                or investigation_producer_witnessed
                or (
                    temporal_setup_requirements_observed
                    and len(temporal_setup_consumed) == len(temporal_setup_actions)
                )
            )
            and trace_setup_owned
            and (
                policy.local_progress
                in {
                    LocalProgressKind.TRACE_SETUP,
                    LocalProgressKind.REARM,
                    LocalProgressKind.THEORY_CORRECTIVE,
                    LocalProgressKind.TEMPORAL_EDGE,
                }
                or requirements_reached
                or temporal_setup_requirements_observed
            )
            and declared_boundary_preserved
            and route_owned
        )
        if local_progress_reached:
            gate_events.append(
                PilotGateEvent(
                    "theory-local-progress",
                    "declared inputs reached their local boundary",
                    evidence={
                        "kind": policy.local_progress.value,
                        "changed": changed,
                        "requirements_reached": requirements_reached,
                        "expectation_boundaries_preserved": expectation_boundaries_preserved,
                        "declared_boundary_preserved": declared_boundary_preserved,
                        "selected_trace_setup": selected_trace_setup,
                        "declared_lifetime": declared_lifetime is not None,
                        "selected_effect_consumed": selected_effect_consumed,
                        "investigation_producer_witnessed": (investigation_producer_witnessed),
                        "investigation_producer_matching_writes": (
                            investigation_producer_receipt.matching_writes
                        ),
                        "intrascan_configuration_consumed": (intrascan_configuration_consumed),
                        "temporal_setup_consumed": temporal_setup_consumed,
                        "temporal_setup_requirements_observed": (
                            temporal_setup_requirements_observed
                        ),
                        "temporal_setup_observations": temporal_setup_observation_receipts,
                        "route_owned": route_owned,
                        "route_distance_before": frame.distance_before,
                        "route_distance_after": landing_distance,
                        "route_blockers": tuple(
                            (crossing.tag, repr(crossing.predicate), crossing.write.scan_id)
                            for crossing in route_blockers
                        ),
                    },
                )
            )
            assessment = TrialAssessment(
                agency=Agency.PILOT,
                bearing=BearingEffect.SATISFIED,
                progress=ProgressEffect.UNCHANGED,
                new_frontier=False,
                accepted=True,
            )
            return _AttemptResult(
                trial=_accepted_trial(
                    attempt,
                    frame,
                    gate_events,
                    channel_motion,
                    earned_work_receipt,
                    AssessedMotion(
                        new_key=trial.key,
                        trend=frame.distance_before,
                        assessment=assessment,
                    ),
                    investigation_producer=investigation_producer_receipt.receipt,
                ),
                gate_events=tuple(gate_events),
                nogood_pairs=frozenset(collected_nogoods),
                confirmed_correction=trial.confirmed_correction,
                avoid_names=tuple(avoid_violations),
            )
        if not (
            policy.local_progress is LocalProgressKind.TRACE_SETUP
            and not declared_boundary_preserved
        ):
            gate_events.append(
                PilotGateEvent(
                    "theory-local-progress-rejected",
                    "declared inputs did not reach their local boundary",
                    evidence={
                        "kind": policy.local_progress.value,
                        "changed": changed,
                        "requirements_reached": requirements_reached,
                        "expectation_boundaries_preserved": expectation_boundaries_preserved,
                        "declared_boundary_preserved": declared_boundary_preserved,
                        "selected_trace_setup": selected_trace_setup,
                        "declared_lifetime": declared_lifetime is not None,
                        "selected_effect_consumed": selected_effect_consumed,
                        "intrascan_configuration_consumed": (intrascan_configuration_consumed),
                        "temporal_setup_consumed": temporal_setup_consumed,
                        "temporal_setup_requirements_observed": (
                            temporal_setup_requirements_observed
                        ),
                        "temporal_setup_observations": temporal_setup_observation_receipts,
                        "route_owned": route_owned,
                        "route_distance_before": frame.distance_before,
                        "route_distance_after": landing_distance,
                        "route_blockers": tuple(
                            (crossing.tag, repr(crossing.predicate), crossing.write.scan_id)
                            for crossing in route_blockers
                        ),
                    },
                )
            )
            return _reject(
                nogoods=(policy.regression_nogoods if not trace_setup_owned else ()),
                proof_rejection=True,
            )

    # Read the selected effect before generic spin/dead-end judgment.  A
    # proved violation is occurrence evidence, not an action nogood; Phase 4
    # may turn it into a narrower requirement without changing producers.
    # ``attempt.effect_observations`` was classified before entry to these
    # generic gates. Phase 3 carries and records it separately; Phase 4 owns
    # turning a proved violation into an actionable requirement. It is not a
    # gate event, rejection, or nogood yet.
    expectation = bearing.expectation
    if (
        expectation is not None
        and fulfilled_expectation_observations(
            expectation,
            attempt.effect_observations,
        )
        and any(
            obligation.boundary is not None
            and not _values_match(
                trial.snap.get(obligation.boundary[0]),
                obligation.boundary[1],
            )
            for obligation in expectation.obligations
        )
    ):
        boundaries = tuple(
            obligation.boundary
            for obligation in expectation.obligations
            if obligation.boundary is not None
        )
        departed_boundary = (
            boundaries[0]
            if len(boundaries) == 1
            and not _values_match(trial.snap.get(boundaries[0][0]), boundaries[0][1])
            else None
        )
        if departed_boundary is not None:
            channel_motion = ChannelMotion(
                departed_boundary[0],
                departed_boundary[1],
                departed_boundary,
                stop_reason="departed",
            )
        assessment = TrialAssessment(
            agency=Agency.PILOT,
            bearing=(
                BearingEffect.DEPARTED if channel_motion.departed else BearingEffect.SATISFIED
            ),
            progress=(
                ProgressEffect.BACKWARD if channel_motion.departed else ProgressEffect.UNCHANGED
            ),
            new_frontier=False,
            accepted=True,
        )
        gate_events.append(
            PilotGateEvent(
                "expectation-survived",
                "selected whole-shape effect reached its exact obliged consumer",
            )
        )
        return _AttemptResult(
            trial=_accepted_trial(
                attempt,
                frame,
                gate_events,
                channel_motion,
                earned_work_receipt,
                AssessedMotion(
                    new_key=trial.key,
                    trend=frame.distance_before,
                    assessment=assessment,
                ),
            ),
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(avoid_violations),
        )

    spin_verdict = _trial_gates._gate_spin(
        trial,
        frame,
        state,
        gate_events=gate_events,
        earned_work_receipt=earned_work_receipt,
    )
    if spin_verdict is _trial_gates._SpinVerdict.SPIN:
        if nogood_pair is not None:
            collected_nogoods.append(nogood_pair)
        _trial_gates._record_gate(
            "SPIN",
            gate_events=gate_events,
            evidence={
                "frame_key": frame.key,
                "trial_key": trial.key,
                "post_pulse_key": trial.post_pulse_key,
                "pending_effects": False,
                "ordinal_advanced": False,
                "actions": applied_actions,
            },
        )
        return _reject()
    if spin_verdict is _trial_gates._SpinVerdict.EXCURSION:
        return _AttemptResult(
            trial=None,
            excursion_attempt=attempt,
            gate_events=tuple(gate_events),
            # An excursion is evidence of a real transient effect.  It is not a
            # spin and therefore never rejects this action on detection.
            nogood_pairs=frozenset(),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(avoid_violations),
        )
    return _verify_after_spin(
        attempt,
        frame,
        state,
        ctx,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        avoid_names=avoid_violations,
        earned_work_receipt=earned_work_receipt,
        channel_motion=channel_motion,
        source_world_key=_executed_source_world_key(frame, state),
    )


def verify_gates(
    attempt: _ExecutedAttempt,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _AttemptResult:
    """Verify one execution while preserving its disposable landing.

    Rejection is a judgment, not destruction of evidence.  Keeping the exact
    executed attempt lets a bounded orient-phase composer inspect or re-orient
    the counterfactual fork without authorizing a commit or a global nogood.
    """

    return replace(
        _verify_gates(attempt, frame, state, ctx),
        executed=attempt,
    )
