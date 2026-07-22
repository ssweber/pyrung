"""Judge an executed fork before it may replace the current world.

``verify_gates`` applies avoid and target checks, rejects spins, visited states,
and dead ends, then delegates motion attribution and progress classification to
``outcome.py``. A suspicious excursion may be replayed before the final verdict.

Passing these gates makes a trial eligible for commit and progress monitoring;
it does not guarantee that later assessment will retain the committed world.
Gate diagnostics are recorded as ``PilotGateEvent`` values on the attempt.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _avoid_snap_names,
    _has_pending_effects,
    _pilot_world_key,
)
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.investigate import investigate_excursion
from pyrung.core.analysis.pilot.navigation import (
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
)
from pyrung.core.analysis.pilot.navigation_evidence import (
    NavigationEvidence,
    Reachable,
)
from pyrung.core.analysis.pilot.outcome import assess_outcome
from pyrung.core.analysis.pilot.trace import target_reached, trace_back
from pyrung.core.analysis.pilot.types import (
    PilotGateEvent,
    _ActionPair,
    _AttemptResult,
    _ExecutedAttempt,
    _PulseState,
    _TrialResult,
)
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class _DeadEndResult:
    tree: Any
    trend: int
    has_new_frontier: bool = False


def _owned_bearing_stop_reason(
    trial: _PulseState,
    channel_tag: str | None,
    target_value: Any,
) -> str | None:
    """Interpret a raw coast receipt against verification's selected owner.

    An inner advance seek arms the outer route channel as its departure bump.
    When that outer channel lands exactly on its requested value, the raw inner
    receipt necessarily says ``departed`` even though the selected outer
    operation was reached.  Rebase that one observation here; relational inner
    boundaries retain their own ``reached`` receipt even when their scalar
    heading was crossed rather than equalled.
    """
    receipt = trial.coast_receipt
    if receipt is None:
        return None
    if channel_tag is not None and _values_match(trial.snap.get(channel_tag), target_value):
        return "reached"
    return receipt.stop_reason


def _trial_result(
    attempt: _ExecutedAttempt,
    frame: Any,
    observe_label: str,
    gate_events: list[PilotGateEvent],
    bearing_stop_reason: str | None,
) -> _TrialResult:
    """Preserve one executed attempt as verification's accepted receipt."""
    trial = attempt.pulse
    intent = attempt.intent
    return _TrialResult(
        fork=trial.fork,
        scan_before=trial.scan_before,
        candidate=dict(intent.action_pairs),
        applied=intent.applied,
        before_snap=frame.snap,
        post_pulse_snap=trial.post_pulse_snap,
        fork_snap=trial.snap,
        observe_label=observe_label,
        bearing_objective=intent.bearing_objective,
        route_prescribed=intent.route_prescribed,
        motion=intent.motion,
        regression_nogoods=intent.regression_nogoods,
        chase_regression_causes=intent.chase_regression_causes,
        gate_events=tuple(gate_events),
        zoom_channel_tag=intent.channel_tag,
        zoom_target_value=intent.channel_target,
        bearing_stop_reason=bearing_stop_reason,
        coast_receipt=trial.coast_receipt,
        timeline=trial.timeline,
    )


# ---------------------------------------------------------------------------
# Gate helpers — excursion diagnosis and retry
# ---------------------------------------------------------------------------


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
    action_pairs: tuple[_ActionPair, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
    avoid_names: list[str],
) -> _PulseState | None:
    key_config = state.key_config
    assert key_config is not None

    if trial.key != frame.key or _has_pending_effects(trial.fork):
        return trial

    # The search key threshold-masks event-earned progress sources, so a trial
    # that advanced one (the knock that bumped a counter the key aliases at
    # ``count < 3``) projects to the same key as doing nothing.  The gauge
    # carries exactly those ordinals: an earn in stride direction is real
    # work, not a spin.
    gauge = getattr(state, "gauge", None)
    if gauge is not None and gauge.ordinal_advanced(frame.snap, trial.snap):
        _record_gate("ORDINAL-ADVANCE", ": gauge earned", gate_events)
        return trial

    if trial.post_pulse_key != frame.key:
        result = investigate_excursion(
            state.work,
            trial.fork,
            frame.snap,
            trial.post_pulse_snap,
            frame.key,
            list(action_pairs),
            cfg=key_config,
            steerable=ctx.steerable,
            rungs=state.rungs,
            resting=ctx.resting,
            edge_tags=ctx.edge_tags,
            scan_budget=ctx.max_scans - state.work.state.scan_id,
            pdg=ctx.pdg,
            program=ctx.program,
            ctx=ctx,
        )
        if result.retry_fork is not None and result.correction is not None:
            retry_snap = dict(result.retry_fork.state.tags)
            retry_rungs = (*state.rungs, *result.correction.rungs)
            if ctx.avoid_pred is not None:
                retry_violations: list[str] = list(_avoid_snap_names(ctx.avoid_pred, retry_snap))
                if not ctx.avoid_pred(frame.snap):
                    for scan in range(trial.scan_before + 1, result.retry_fork.state.scan_id + 1):
                        retry_violations.extend(
                            _avoid_snap_names(
                                ctx.avoid_pred,
                                result.retry_fork.history.at(scan).tags,
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
                    return None
            retry_key = _pilot_world_key(retry_snap, key_config, retry_rungs)
            _record_gate(
                "EXCURSION-RETRY-OK",
                (
                    f": reverted={result.reverted}, "
                    f"rungs={tuple((r.dest, r.value) for r in result.correction.rungs)}"
                ),
                gate_events,
            )
            return _PulseState(
                fork=result.retry_fork,
                scan_before=trial.scan_before,
                action_scan=trial.action_scan,
                action_snap=trial.action_snap,
                wait_snaps=trial.wait_snaps,
                post_pulse_snap=trial.post_pulse_snap,
                post_pulse_key=trial.post_pulse_key,
                snap=retry_snap,
                key=retry_key,
                # The retry fork replaced the original pulse; its recorded
                # session is the timeline this trial carries forward.
                timeline=result.retry_timeline,
                confirmed_correction=result.correction,
            )
        if result.reverted:
            _record_gate("EXCURSION-NO-HOLDS", gate_events=gate_events)
        else:
            _record_gate("EXCURSION-RETRY-FAIL", gate_events=gate_events)
        return None

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
            "actions": action_pairs,
        },
    )
    return None


def _gate_cycle(
    trial: _PulseState,
    frame: Any,
    state: Any,
    *,
    pending: bool,
    influence_prescribed: bool,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
) -> bool:
    if trial.key not in state.seen_keys or pending:
        return True
    # A revisit by the key's lights that advanced an event-earned ordinal is a
    # NEW visit — ``(AtDoor, count=2)`` aliases ``(AtDoor, count=1)`` only in
    # the threshold-masked projection (see _gate_spin's twin check).
    gauge = getattr(state, "gauge", None)
    if gauge is not None and gauge.ordinal_advanced(frame.snap, trial.snap):
        _record_gate("ORDINAL-ADVANCE", ": gauge earned", gate_events)
        return True
    if not influence_prescribed:
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
                "influence_prescribed": influence_prescribed,
            },
        )
        return False
    _record_gate(
        "INFLUENCE-OVERRIDE-CYCLE",
        ": influence-prescribed",
        gate_events,
    )
    return True


def _gate_dead_end(
    trial: _PulseState,
    action_pairs: tuple[_ActionPair, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    influence_prescribed: bool,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
    zoom_channel_tag: str | None = None,
    zoom_target_value: Any = None,
) -> _DeadEndResult | None:
    # A zoom that drove its channel register to the target value (e.g.
    # S_StateCurrent 3->6) is a confirmed advance, even if the global target's
    # onward leg is another dwell that trace_back can't surface.  And a zoom
    # whose channel register *moved away* on its own (an ejection,
    # S_StateCurrent 6->8) is an AMBIENT_DRIFT the investigation must own — not a
    # stall.  Either way the trial must reach outcome classification, not be
    # discarded here; only a true stall (channel unchanged, no frontier) is a
    # dead end.  (For command candidates zoom_channel_tag is None, so this gate
    # is unchanged for them.)
    channel_reached = zoom_channel_tag is not None and _values_match(
        trial.snap.get(zoom_channel_tag), zoom_target_value
    )
    channel_moved = zoom_channel_tag is not None and not _values_match(
        trial.snap.get(zoom_channel_tag), frame.snap.get(zoom_channel_tag)
    )
    accept_override = influence_prescribed or channel_reached or channel_moved
    new_tree = trace_back(
        ctx.target_tag,
        ctx.target_value,
        trial.snap,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        # Same writer ranking as the frame trace, or the trend/frontier this
        # gate computes drifts against the tree the candidate came from.
        clear_only=getattr(ctx, "clear_only", frozenset()),
        opaque_loop=ctx.opaque_loop,
        pipeline_internal_tags=ctx.pipeline_internal_tags,
        route=ctx.route,
        prior=getattr(ctx, "domain_prior", None),
        avoid_pred=ctx.avoid_pred,
        via_pred=ctx.via_pred,
    )
    new_trend = new_tree.unsatisfied_count()
    new_actions = set(new_tree.ordered_actions())
    old_actions = set(frame.tree.ordered_actions())
    action_inputs = set(action_pairs)
    post_frame = replace(frame, snap=trial.snap, tree=new_tree, key=trial.key)
    frontier_status = NavigationEvidence.frontier_status(
        OrientationWorld(
            world_key=trial.key,
            snapshot=trial.snap,
            frame=post_frame,
            state=state,
            context=ctx,
        ),
        TargetSpec(ctx.target_tag, ctx.target_value, ctx.target_predicate),
        NavigationConstraints(ctx.blocked_route_actions, ctx.avoid_pred),
        ctx.compass.knowledge,
    )
    influence_frontier = isinstance(frontier_status, Reachable)
    pending = _has_pending_effects(trial.fork)

    if not new_actions and not influence_frontier and not pending:
        if not accept_override:
            if nogood_pair is not None:
                collected_nogoods.append(nogood_pair)
            _record_gate(
                "DEAD-END",
                ": empty frontier, no pending effects",
                gate_events,
                evidence={
                    "new_actions": tuple(sorted(new_actions, key=repr)),
                    "influence_frontier": influence_frontier,
                    "pending_effects": pending,
                    "influence_prescribed": influence_prescribed,
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
            else "INFLUENCE-OVERRIDE-DEAD-END",
            ": channel target reached"
            if channel_reached
            else ": channel ejected"
            if channel_moved
            else ": influence-prescribed",
            gate_events,
        )
    elif (
        new_actions
        and not (new_actions - action_inputs - old_actions)
        and new_trend >= frame.distance_before
    ):
        # An event-earned ordinal advance is trend improvement the tree can't
        # see: ``count 1 -> 2`` leaves the ``count >= 3`` leaf unsatisfied and
        # the action set unchanged, yet the trial did a third of the work.
        gauge = getattr(state, "gauge", None)
        if gauge is not None and gauge.ordinal_advanced(frame.snap, trial.snap):
            _record_gate("ORDINAL-ADVANCE", ": gauge earned", gate_events)
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
                    "action_inputs": tuple(sorted(action_inputs, key=repr)),
                    "trend_before": frame.distance_before,
                    "trend_after": new_trend,
                    "influence_prescribed": influence_prescribed,
                    "channel_reached": channel_reached,
                    "channel_moved": channel_moved,
                },
            )
            return None
        else:
            _record_gate(
                "CHANNEL-OVERRIDE-LATERAL"
                if (channel_reached or channel_moved)
                else "INFLUENCE-OVERRIDE-LATERAL",
                ": channel target reached"
                if channel_reached
                else ": channel ejected"
                if channel_moved
                else ": influence-prescribed",
                gate_events,
            )

    genuinely_new_actions = bool(new_actions - action_inputs - old_actions)
    old_unsat: set[tuple[str, Any]] = set()
    frame.tree._collect_unsatisfied(old_unsat)
    new_unsat: set[tuple[str, Any]] = set()
    new_tree._collect_unsatisfied(new_unsat)
    genuinely_new_conditions = bool(new_unsat - old_unsat)
    has_new_frontier = genuinely_new_actions or genuinely_new_conditions
    return _DeadEndResult(tree=new_tree, trend=new_trend, has_new_frontier=has_new_frontier)


# ---------------------------------------------------------------------------
# Verify pipeline — the shared gate sequence
# ---------------------------------------------------------------------------


def verify_gates(
    attempt: _ExecutedAttempt,
    frame: Any,
    state: Any,
    ctx: Any,
) -> _AttemptResult:
    """Apply the shared trial gates to an executed pulse or coast.

    Runs avoid and target checks, then spin, cycle, and dead-end gates followed
    by outcome classification. All steering execution modes converge here.
    """
    trial = attempt.pulse
    intent = attempt.intent
    action_pairs = intent.action_pairs
    nogood_pair = intent.nogood_pair
    zoom_channel_tag = intent.channel_tag
    zoom_target_value = intent.channel_target
    gate_events: list[PilotGateEvent] = []
    collected_nogoods: list[_ActionPair] = []
    retry_avoid_names: list[str] = []
    bearing_stop_reason = _owned_bearing_stop_reason(
        trial,
        zoom_channel_tag,
        zoom_target_value,
    )

    # ── Scan gate (avoid=) ────────────────────────────────────────────────
    # Settled state first (the original veto: never rest in the avoided region).
    # Then transient coverage: a trial that started clear but blips the avoided
    # condition true mid-trial — the pulse scan or any coast snapshot — is
    # rejected too, so there is no "two-scan wink" where avoid is true mid-coast
    # and false again by settlement.  Both arms nogood the choice and record the
    # violated names for the terminal decline.
    if ctx.avoid_pred is not None:
        settled = _avoid_snap_names(ctx.avoid_pred, trial.snap)
        if settled:
            gate_events.append(
                PilotGateEvent("avoid", f"settled state matches avoid: {', '.join(settled)}")
            )
            return _AttemptResult(
                trial=None,
                gate_events=tuple(gate_events),
                nogood_pairs=frozenset({nogood_pair}) if nogood_pair is not None else frozenset(),
                avoid_names=tuple(settled),
            )
        if not ctx.avoid_pred(frame.snap):
            for snap in (trial.action_snap, *trial.wait_snaps, trial.post_pulse_snap):
                wink = _avoid_snap_names(ctx.avoid_pred, snap)
                if wink:
                    gate_events.append(
                        PilotGateEvent("avoid", f"transient scan enters avoid: {', '.join(wink)}")
                    )
                    return _AttemptResult(
                        trial=None,
                        gate_events=tuple(gate_events),
                        nogood_pairs=(
                            frozenset({nogood_pair}) if nogood_pair is not None else frozenset()
                        ),
                        avoid_names=tuple(wink),
                    )

    if target_reached(trial.snap, ctx.target_tag, ctx.target_value, ctx.target_predicate):
        gate_events.append(PilotGateEvent("target", f"{ctx.target_tag}={ctx.target_value!r}"))
        return _AttemptResult(
            trial=_trial_result(
                attempt,
                frame,
                intent.target_observe_label,
                gate_events,
                bearing_stop_reason,
            ),
            gate_events=tuple(gate_events),
        )

    spun = _gate_spin(
        trial,
        action_pairs,
        frame,
        state,
        ctx,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        avoid_names=retry_avoid_names,
    )
    if spun is None:
        return _AttemptResult(
            trial=None,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            avoid_names=tuple(retry_avoid_names),
        )
    trial = spun
    attempt = replace(attempt, pulse=trial)

    if target_reached(trial.snap, ctx.target_tag, ctx.target_value, ctx.target_predicate):
        gate_events.append(PilotGateEvent("target", f"{ctx.target_tag}={ctx.target_value!r}"))
        return _AttemptResult(
            trial=_trial_result(
                attempt,
                frame,
                intent.target_observe_label,
                gate_events,
                bearing_stop_reason,
            ),
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(retry_avoid_names),
        )

    pending = _has_pending_effects(trial.fork)
    if not _gate_cycle(
        trial,
        frame,
        state,
        pending=pending,
        influence_prescribed=intent.influence_prescribed,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
    ):
        return _AttemptResult(
            trial=None,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(retry_avoid_names),
        )

    dead_end = _gate_dead_end(
        trial,
        action_pairs,
        frame,
        state,
        ctx,
        influence_prescribed=intent.influence_prescribed,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
        collected_nogoods=collected_nogoods,
        zoom_channel_tag=zoom_channel_tag,
        zoom_target_value=zoom_target_value,
    )
    if dead_end is None:
        return _AttemptResult(
            trial=None,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(retry_avoid_names),
        )

    assessment = assess_outcome(
        trial,
        action_pairs,
        frame,
        ctx,
        dead_end.trend,
        dead_end.has_new_frontier,
        chase_cause_roots,
        route_prescribed=intent.route_prescribed,
        zoom_channel_tag=zoom_channel_tag,
        zoom_target_value=zoom_target_value,
        zoom_progressed=(
            getattr(state, "gauge", None) is not None
            and state.gauge.ordinal_advanced(frame.snap, trial.snap)
        ),
        zoom_stop_reason=(bearing_stop_reason),
    )

    outcome = assessment.legacy_outcome
    if not assessment.accepted:
        if nogood_pair is not None:
            collected_nogoods.append(nogood_pair)
        _record_gate(
            "ZOOM-STALL" if zoom_channel_tag is not None else "BAD-EDGE",
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
                "zoom_channel_tag": zoom_channel_tag,
                "zoom_target_value": zoom_target_value,
                "zoom_actual_value": (
                    trial.snap.get(zoom_channel_tag) if zoom_channel_tag is not None else None
                ),
            },
        )
        return _AttemptResult(
            trial=None,
            gate_events=tuple(gate_events),
            nogood_pairs=frozenset(collected_nogoods),
            confirmed_correction=trial.confirmed_correction,
            avoid_names=tuple(retry_avoid_names),
        )

    gate_events.append(
        PilotGateEvent(outcome.value, f"distance {frame.distance_before} -> {dead_end.trend}")
    )

    return _AttemptResult(
        trial=replace(
            _trial_result(
                attempt,
                frame,
                intent.observe_label,
                gate_events,
                bearing_stop_reason,
            ),
            new_key=trial.key,
            trend=dead_end.trend,
            outcome=outcome,
            assessment=assessment,
        ),
        gate_events=tuple(gate_events),
        nogood_pairs=frozenset(collected_nogoods),
        confirmed_correction=trial.confirmed_correction,
        avoid_names=tuple(retry_avoid_names),
    )
