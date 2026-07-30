"""Judge an executed fork before it may replace the current world.

``verify_gates`` applies avoid and target checks, rejects spins, visited states,
and dead ends, then delegates motion attribution and progress classification to
``outcome.py``. It reports a suspicious excursion to the drive loop without
performing runtime investigation. ``verify_excursion_retry`` judges the one
replay returned by that owner and resumes after the spin gate.

Passing these gates makes a trial eligible for commit and progress monitoring;
it does not guarantee that later assessment will retain the committed world.
Gate diagnostics are recorded as ``PilotGateEvent`` values on the attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.avoid import _avoid_snap_names
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.coast import _has_pending_effects
from pyrung.core.analysis.pilot.constrained_reachability import (
    NavigationEvidence,
    Reachable,
)
from pyrung.core.analysis.pilot.earned_work import EarnedWorkMovement, EarnedWorkReceipt
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
)
from pyrung.core.analysis.pilot.outcome import assess_outcome
from pyrung.core.analysis.pilot.trace import TraceReadConstraints, target_reached, trace_back
from pyrung.core.analysis.pilot.types import (
    AssessedMotion,
    ChannelMotion,
    MotionKind,
    PilotGateEvent,
    TargetReached,
    _AcceptedTrial,
    _ActionPair,
    _AttemptResult,
    _ExecutedAttempt,
    _ExecutionEvidence,
    _PulseState,
)
from pyrung.core.analysis.pilot.world_key import _pilot_world_key
from pyrung.core.analysis.sp_values import _values_match

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.investigate import ExcursionResult


@dataclass(frozen=True)
class _DeadEndResult:
    tree: Any
    trend: int
    has_new_frontier: bool = False


class _SpinVerdict(Enum):
    """Local spin-gate judgment; orchestration acts on excursions elsewhere."""

    PASS = auto()
    SPIN = auto()
    EXCURSION = auto()


def _avoid_names_after_clear(
    avoid: Any,
    start: dict[str, Any],
    observed: dict[str, Any],
) -> tuple[str, ...]:
    """Avoid members clear at *start* that fire in one later observation.

    Compiled unions own the per-member distinction. A bare callable has only
    aggregate identity, so starting true exempts its later snapshots.
    """

    violated_after_clear = getattr(avoid, "violated_after_clear", None)
    if violated_after_clear is not None:
        return tuple(violated_after_clear(start, observed))
    return () if bool(avoid(start)) else _avoid_snap_names(avoid, observed)


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


def _accepted_trial(
    attempt: _ExecutedAttempt,
    frame: Any,
    gate_events: list[PilotGateEvent],
    channel_motion: ChannelMotion,
    earned_work_receipt: EarnedWorkReceipt,
    verification: TargetReached | AssessedMotion,
) -> _AcceptedTrial:
    """Preserve the final executed attempt and its PLC-free evidence."""
    pulse = attempt.pulse
    execution = _ExecutionEvidence(
        before_snap=frame.snap,
        after_snap=pulse.snap,
        channel_motion=channel_motion,
        coast_receipt=pulse.coast_receipt,
        timeline=pulse.timeline,
    )
    return _AcceptedTrial(
        attempt=attempt,
        execution=execution,
        earned_work_receipt=earned_work_receipt,
        gate_events=tuple(gate_events),
        verification=verification,
    )


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------


def _record_gate(
    event: str,
    detail: str = "",
    gate_events: list[PilotGateEvent] | None = None,
    *,
    evidence: dict[str, Any] | None = None,
) -> None:
    if gate_events is not None:
        gate_events.append(
            PilotGateEvent(
                event=event.lower(),
                detail=detail.lstrip(": "),
                evidence=evidence or {},
            )
        )


def _gate_spin(
    trial: _PulseState,
    frame: Any,
    state: Any,
    *,
    gate_events: list[PilotGateEvent],
    earned_work_receipt: EarnedWorkReceipt | None = None,
) -> _SpinVerdict:
    """Classify one settled execution without investigating or retrying it."""
    if trial.key != frame.key or _has_pending_effects(trial.fork):
        return _SpinVerdict.PASS

    # The search key threshold-masks event-earned progress sources, so a trial
    # that advanced one (the knock that incremented a counter the key aliases at
    # ``count < 3``) projects to the same key as doing nothing.  Earned work
    # carries exactly those ordinals: an earn in stride direction is real
    # work, not a spin.
    if earned_work_receipt is None:
        earned_work = getattr(state, "earned_work", None)
        earned_work_receipt = (
            earned_work.receipt(frame.snap, trial.snap)
            if earned_work is not None
            else EarnedWorkReceipt()
        )
    if earned_work_receipt.any_forward:
        _record_gate("ORDINAL-ADVANCE", ": earned work advanced", gate_events)
        return _SpinVerdict.PASS

    if trial.post_pulse_key != frame.key:
        return _SpinVerdict.EXCURSION

    return _SpinVerdict.SPIN


def _gate_cycle(
    trial: _PulseState,
    state: Any,
    *,
    pending: bool,
    earned_work_receipt: EarnedWorkReceipt,
    learned_prescribed: bool,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
) -> bool:
    if trial.key not in state.seen_keys or pending:
        return True
    # A revisit by the key's lights that advanced an event-earned ordinal is a
    # NEW visit — ``(AtDoor, count=2)`` aliases ``(AtDoor, count=1)`` only in
    # the threshold-masked projection (see _gate_spin's twin check).
    if earned_work_receipt.any_forward:
        _record_gate("ORDINAL-ADVANCE", ": earned work advanced", gate_events)
        return True
    if not learned_prescribed:
        if nogood_pair is not None:
            collected_nogoods.append(nogood_pair)
        _record_gate(
            "CYCLE",
            gate_events=gate_events,
            evidence={
                "trial_key": trial.key,
                "seen": True,
                "pending_effects": pending,
                "ordinal_advanced": False,
                "learned_prescribed": learned_prescribed,
            },
        )
        return False
    _record_gate(
        "LEARNED-OVERRIDE-CYCLE",
        ": learned-prescribed",
        gate_events,
    )
    return True


def _gate_dead_end(
    trial: _PulseState,
    applied_actions: tuple[_ActionPair, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    target: TargetSpec,
    earned_work_receipt: EarnedWorkReceipt,
    learned_prescribed: bool,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
    channel_motion: ChannelMotion,
) -> _DeadEndResult | None:
    """Reject a trial that moved nothing and can prove no pending motion.

    The post-trial trace here deliberately omits the harness coupling-driver
    proposal model that planning may use: continued physical motion needs
    executed evidence or a live pending effect on the exact trial fork.
    """
    # A bearing coast that drove its channel register to the target value (e.g.
    # S_StateCurrent 3->6) is a confirmed advance, even if the global target's
    # onward leg is another dwell that trace_back can't surface. A bearing coast
    # whose channel register *moved away* (an ejection, S_StateCurrent 6->8)
    # likewise belongs to post-commit handling, whether attribution proves
    # program, PILOT, or unknown agency. Either landing must reach outcome
    # classification; only a true stall (channel unchanged, no frontier) is a
    # dead end. (For command candidates the channel receipt is inactive, so
    # this gate is unchanged for them.)
    channel_reached = channel_motion.reached
    channel_moved = channel_motion.departed
    accept_override = learned_prescribed or channel_reached or channel_moved
    new_tree = trace_back(
        target.tag,
        target.value,
        trial.snap,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        # Same writer ranking as the frame trace, or the trend/frontier this
        # gate computes drifts against the tree the candidate came from.
        # Deliberately omit the live harness: Orientation may use its coupling
        # model to propose a driver, but post-trial proof comes only from
        # executed evidence or _has_pending_effects(trial.fork) below.
        constraints=TraceReadConstraints(
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            route=ctx.route,
            prior=ctx.domain_prior,
            avoid_pred=ctx.avoid_pred,
        ),
    )
    new_trend = new_tree.unsatisfied_count()
    new_actions = set(new_tree.ordered_actions())
    old_actions = set(frame.tree.ordered_actions())
    applied_inputs = set(applied_actions)
    post_frame = replace(frame, snap=trial.snap, tree=new_tree, key=trial.key)
    frontier_status = NavigationEvidence.frontier_status(
        OrientationWorld(
            world_key=trial.key,
            snapshot=trial.snap,
            frame=post_frame,
            state=state,
            context=ctx,
        ),
        target,
        NavigationConstraints(
            blocked_actions=ctx.blocked_actions,
            avoid_predicate=ctx.avoid_pred,
        ),
        ctx.compass.knowledge,
    )
    reachable_frontier = isinstance(frontier_status, Reachable)
    pending = _has_pending_effects(trial.fork)

    if not new_actions and not reachable_frontier and not pending:
        if not accept_override:
            if nogood_pair is not None:
                collected_nogoods.append(nogood_pair)
            _record_gate(
                "DEAD-END",
                ": empty frontier, no pending effects",
                gate_events,
                evidence={
                    "new_actions": tuple(sorted(new_actions, key=repr)),
                    "reachable_frontier": reachable_frontier,
                    "pending_effects": pending,
                    "learned_prescribed": learned_prescribed,
                    "channel_reached": channel_reached,
                    "channel_moved": channel_moved,
                    "trend_before": frame.distance_before,
                    "trend_after": new_trend,
                },
            )
            return None
        _record_gate(
            "CHANNEL-OVERRIDE-DEAD-END"
            if (channel_reached or channel_moved)
            else "LEARNED-OVERRIDE-DEAD-END",
            ": channel target reached"
            if channel_reached
            else ": channel ejected"
            if channel_moved
            else ": learned-prescribed",
            gate_events,
        )
    elif (
        new_actions
        and not (new_actions - applied_inputs - old_actions)
        and new_trend >= frame.distance_before
    ):
        # An event-earned ordinal advance is trend improvement the tree can't
        # see: ``count 1 -> 2`` leaves the ``count >= 3`` leaf unsatisfied and
        # the action set unchanged, yet the trial did a third of the work.
        if earned_work_receipt.any_forward:
            _record_gate("ORDINAL-ADVANCE", ": earned work advanced", gate_events)
        elif not accept_override:
            if nogood_pair is not None:
                collected_nogoods.append(nogood_pair)
            _record_gate(
                "LATERAL",
                ": no new frontier, no trend improvement",
                gate_events,
                evidence={
                    "new_actions": tuple(sorted(new_actions, key=repr)),
                    "old_actions": tuple(sorted(old_actions, key=repr)),
                    "action_inputs": tuple(sorted(applied_inputs, key=repr)),
                    "trend_before": frame.distance_before,
                    "trend_after": new_trend,
                    "learned_prescribed": learned_prescribed,
                    "channel_reached": channel_reached,
                    "channel_moved": channel_moved,
                },
            )
            return None
        else:
            _record_gate(
                "CHANNEL-OVERRIDE-LATERAL"
                if (channel_reached or channel_moved)
                else "LEARNED-OVERRIDE-LATERAL",
                ": channel target reached"
                if channel_reached
                else ": channel ejected"
                if channel_moved
                else ": learned-prescribed",
                gate_events,
            )

    genuinely_new_actions = bool(new_actions - applied_inputs - old_actions)
    old_unsat = frame.tree.unsatisfied_conditions()
    new_unsat = new_tree.unsatisfied_conditions()
    genuinely_new_conditions = bool(new_unsat - old_unsat)
    has_new_frontier = genuinely_new_actions or genuinely_new_conditions
    return _DeadEndResult(tree=new_tree, trend=new_trend, has_new_frontier=has_new_frontier)


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

    pending = _has_pending_effects(trial.fork)
    if not _gate_cycle(
        trial,
        state,
        pending=pending,
        earned_work_receipt=earned_work_receipt,
        learned_prescribed=policy.learned_prescribed,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
    ):
        return _reject()

    dead_end = _gate_dead_end(
        trial,
        policy.applied,
        frame,
        state,
        ctx,
        target=bearing.objective.target,
        earned_work_receipt=earned_work_receipt,
        learned_prescribed=policy.learned_prescribed,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        channel_motion=channel_motion,
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
        chase_cause_roots,
        route_prescribed=policy.route_prescribed,
        channel_motion=channel_motion,
        earned_work_receipt=earned_work_receipt,
    )

    if not assessment.accepted:
        if nogood_pair is not None:
            collected_nogoods.append(nogood_pair)
        _record_gate(
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
            ),
        ),
        gate_events=tuple(gate_events),
        nogood_pairs=frozenset(collected_nogoods),
        confirmed_correction=trial.confirmed_correction,
        observations=observations,
        avoid_names=tuple(avoid_names),
    )


def verify_excursion_retry(
    detected_result: _AttemptResult,
    investigation_result: ExcursionResult,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _AttemptResult:
    """Judge one drive-loop-owned excursion replay, then resume after spin.

    The original attempt's observations and partial gate history survive
    unchanged.  The replay is checked against every retained history snapshot
    for ``avoid=`` before its exact correction, timeline, key, and earned-work
    receipt may proceed through the remaining gates.
    """
    attempt = detected_result.excursion_attempt
    if attempt is None:
        raise ValueError("excursion retry requires the detected executed attempt")

    trial = attempt.pulse
    policy = attempt.bearing.act.policy
    nogood_pair = policy.nogood_pair
    gate_events = list(detected_result.gate_events)
    collected_nogoods = list(detected_result.nogood_pairs)
    avoid_names = list(detected_result.avoid_names)
    observations = detected_result.observations

    if investigation_result.retry_fork is None or investigation_result.correction is None:
        _record_gate(
            "EXCURSION-NO-HOLDS" if investigation_result.reverted else "EXCURSION-RETRY-FAIL",
            gate_events=gate_events,
        )
        return _AttemptResult(
            trial=None,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=detected_result.confirmed_correction,
            observations=observations,
            avoid_names=tuple(avoid_names),
        )

    key_config = state.key_config
    assert key_config is not None
    retry_fork = investigation_result.retry_fork
    correction = investigation_result.correction
    retry_snap = dict(retry_fork.state.tags)
    retry_pilot_rungs = (*state.pilot_rungs, *correction.pilot_rungs)

    if ctx.avoid_pred is not None:
        retry_violations: list[str] = list(_avoid_snap_names(ctx.avoid_pred, retry_snap))
        for scan in range(trial.scan_before + 1, retry_fork.state.scan_id + 1):
            retry_violations.extend(
                _avoid_names_after_clear(
                    ctx.avoid_pred,
                    frame.snap,
                    dict(retry_fork.history.at(scan).tags),
                )
            )
        if retry_violations:
            names = tuple(dict.fromkeys(retry_violations))
            avoid_names.extend(names)
            if nogood_pair is not None:
                collected_nogoods.append(nogood_pair)
            _record_gate(
                "AVOID",
                f": excursion retry enters avoid: {', '.join(names)}",
                gate_events,
            )
            return _AttemptResult(
                trial=None,
                gate_events=tuple(gate_events),
                nogood_pairs=frozenset(collected_nogoods),
                confirmed_correction=detected_result.confirmed_correction,
                observations=observations,
                avoid_names=tuple(avoid_names),
            )

    retry_key = _pilot_world_key(retry_snap, key_config, retry_pilot_rungs)
    _record_gate(
        "EXCURSION-RETRY-OK",
        (
            f": reverted={investigation_result.reverted}, "
            f"pilot_rungs={tuple((r.dest, r.value) for r in correction.pilot_rungs)}"
        ),
        gate_events,
    )
    retry_trial = replace(
        trial,
        fork=retry_fork,
        snap=retry_snap,
        key=retry_key,
        timeline=investigation_result.retry_timeline,
        confirmed_correction=correction,
    )
    retry_attempt = replace(attempt, pulse=retry_trial)
    earned_work = getattr(state, "earned_work", None)
    earned_work_receipt = (
        earned_work.receipt(frame.snap, retry_snap)
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
    channel_motion = _owned_channel_motion(
        trial,
        trial.channel_motion if trial.channel_motion.active else declared_motion,
    )
    return _verify_after_spin(
        retry_attempt,
        frame,
        state,
        ctx,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        avoid_names=avoid_names,
        earned_work_receipt=earned_work_receipt,
        channel_motion=channel_motion,
        observations=observations,
    )


def verify_gates(
    attempt: _ExecutedAttempt,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _AttemptResult:
    """Apply the shared trial gates to an executed pulse or coast.

    Runs avoid and target checks, then spin, cycle, and dead-end gates followed
    by outcome classification. Condition-like avoids fired by a folded coast
    arrive on its receipt; opaque callables are checked only at endpoints and
    real snapshots retained by execution. All steering execution modes
    converge here.

    Verification owns the accepted trial's earned-work receipt. An excursion
    returns its exact attempt to the drive loop; the retry judge computes the
    replacement receipt before resuming this sequence after spin.
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
    retry_avoid_names: list[str] = []
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
    ) -> _AttemptResult:
        return _AttemptResult(
            trial=None,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods if nogoods is None else nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(retry_avoid_names if avoid_names is None else avoid_names),
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
            avoid_names=tuple(retry_avoid_names),
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
            wink = _avoid_names_after_clear(ctx.avoid_pred, frame.snap, snap)
            if wink:
                gate_events.append(
                    PilotGateEvent("avoid", f"transient scan enters avoid: {', '.join(wink)}")
                )
                return _reject(
                    nogoods=({nogood_pair} if nogood_pair is not None else ()),
                    avoid_names=wink,
                )

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

    spin_verdict = _gate_spin(
        trial,
        frame,
        state,
        gate_events=gate_events,
        earned_work_receipt=earned_work_receipt,
    )
    if spin_verdict is _SpinVerdict.SPIN:
        if nogood_pair is not None:
            collected_nogoods.append(nogood_pair)
        _record_gate(
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
    if spin_verdict is _SpinVerdict.EXCURSION:
        return _AttemptResult(
            trial=None,
            excursion_attempt=attempt,
            gate_events=tuple(gate_events),
            # An excursion is evidence of a real transient effect.  It is not a
            # spin and therefore never rejects this action on detection.
            nogood_pairs=frozenset(),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(retry_avoid_names),
        )
    return _verify_after_spin(
        attempt,
        frame,
        state,
        ctx,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        avoid_names=retry_avoid_names,
        earned_work_receipt=earned_work_receipt,
        channel_motion=channel_motion,
    )
