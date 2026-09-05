"""Stateless local judgments for one executed Pilot trial.

These gates classify exact execution evidence and append immutable gate events.
They do not sequence verification, construct accepted receipts, investigate an
excursion, replay an attempt, or adopt a world.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import Any

from pyrung.core.analysis.pilot.avoid import _avoid_snap_names
from pyrung.core.analysis.pilot.coast import _has_pending_effects
from pyrung.core.analysis.pilot.constrained_reachability import NavigationEvidence, Reachable
from pyrung.core.analysis.pilot.earned_work import (
    EarnedWorkReceipt,
    earned_work_is_useful_motion,
)
from pyrung.core.analysis.pilot.effect_observation import effect_reached_consumer
from pyrung.core.analysis.pilot.execution import ChannelMotion
from pyrung.core.analysis.pilot.navigation_contracts import (
    NavigationConstraints,
    OrientationWorld,
    TargetSpec,
    _ActionPair,
)
from pyrung.core.analysis.pilot.trace import trace_target
from pyrung.core.analysis.pilot.trace_read import TraceReadConstraints
from pyrung.core.analysis.pilot.trace_tree import frontier_pairs
from pyrung.core.analysis.pilot.types import (
    PilotGateEvent,
    RevisitCredential,
    _ExecutedAttempt,
    _PulseState,
)
from pyrung.core.analysis.prove.expr import _eval_expr_from_state
from pyrung.core.analysis.sp_values import _values_match
from pyrung.core.instruction.advance import constraint_holds


@dataclass(frozen=True)
class _DeadEndResult:
    tree: Any
    trend: int
    has_new_frontier: bool = False
    advanced_frontier: tuple[_ActionPair, ...] = ()


class _SpinVerdict(Enum):
    """Local spin-gate judgment; orchestration acts on excursions elsewhere."""

    PASS = auto()
    SPIN = auto()
    EXCURSION = auto()


_PROVED_EFFECT_VIOLATIONS = frozenset({"ABSENT", "OVERWRITTEN", "STRANDED", "DISPLACED"})


def _proved_effect_violations(attempt: _ExecutedAttempt) -> tuple[Any, ...]:
    """Exact selected-effect failures which must outrank generic acceptance."""

    fulfilled_obligations = {
        id(item.obligation) for item in attempt.effect_observations if effect_reached_consumer(item)
    }
    return tuple(
        observation
        for observation in attempt.effect_observations
        if observation.disposition in _PROVED_EFFECT_VIOLATIONS
        and (observation.obligation.consumer is not None or observation.obligation.terminal_target)
        and id(observation.obligation) not in fulfilled_obligations
    )


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
    """Classify one settled execution without investigating or replaying it."""
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
    if earned_work_is_useful_motion(earned_work_receipt):
        _record_gate("ORDINAL-ADVANCE", ": earned work advanced", gate_events)
        return _SpinVerdict.PASS

    if trial.post_pulse_key != frame.key:
        return _SpinVerdict.EXCURSION

    return _SpinVerdict.SPIN


def _gate_revisit(
    trial: _PulseState,
    state: Any,
    *,
    earned_work_receipt: EarnedWorkReceipt,
    earned_credential: RevisitCredential | None,
    departure_credential: RevisitCredential | None,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
) -> bool:
    """Admit a classified landing or reject a repeated executable world.

    Pending transport work and navigation provenance are intentionally absent:
    neither proves target-relative progress. A departure is incident authority
    only once, using its exact source/action/channel occurrence.
    """
    if trial.key not in state.seen_keys:
        return True
    prior_progress = None
    committed = tuple(getattr(state, "committed_acts", ()))
    if committed:
        prior_progress = committed[-1].context.execution.scan_progress
    if prior_progress is not None and prior_progress.landing_scan == trial.scan_before:
        _record_gate(
            "SCAN-FRONTIER",
            ": adjacent scan follows an exact productive tip",
            gate_events=gate_events,
            evidence={
                "productive_scan": prior_progress.productive_scan,
                "landing_scan": prior_progress.landing_scan,
                "kind": prior_progress.kind,
                "selected_act": prior_progress.selected_act,
            },
        )
        return True
    if (
        earned_work_is_useful_motion(earned_work_receipt)
        and earned_credential is not None
        and earned_credential.kind == "earned-work"
        and earned_credential not in state.consumed_revisits
    ):
        _record_gate("ORDINAL-ADVANCE", ": earned work advanced", gate_events)
        return True
    if (
        departure_credential is not None
        and departure_credential.kind == "departure"
        and departure_credential not in state.consumed_revisits
    ):
        _record_gate(
            "DEPARTURE-REVISIT",
            ": novel departure occurrence",
            gate_events=gate_events,
            evidence={
                "trial_key": trial.key,
                "seen": True,
                "ordinal_advanced": False,
                "departure_credential": departure_credential,
                "consumed": False,
            },
        )
        return True
    if nogood_pair is not None:
        collected_nogoods.append(nogood_pair)
    _record_gate(
        "CYCLE",
        gate_events=gate_events,
        evidence={
            "trial_key": trial.key,
            "seen": True,
            "ordinal_advanced": False,
            "earned_credential": earned_credential,
            "earned_work_consumed": (
                earned_credential is not None and earned_credential in state.consumed_revisits
            ),
            "departure_credential": departure_credential,
            "departure_consumed": (
                departure_credential is not None and departure_credential in state.consumed_revisits
            ),
        },
    )
    return False


def _gate_dead_end(
    trial: _PulseState,
    applied_actions: tuple[_ActionPair, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    target: TargetSpec,
    earned_work_receipt: EarnedWorkReceipt,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
    collected_nogoods: list[_ActionPair],
    channel_motion: ChannelMotion,
    accept_temporal_progress: bool = False,
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
    accept_override = (
        channel_reached or channel_moved or earned_work_is_useful_motion(earned_work_receipt)
    )
    new_tree = trace_target(
        target,
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

    # The scan-level receipt is about the selected target frontier, not the
    # trace tree's aggregate size. Retracing can grow while a stepper advances,
    # or shrink because transient table plumbing changed. Count only an exact
    # current-frontier condition that was false at S0 and is true at the retained
    # scan tip.
    def _frontier_holds(pair: _ActionPair) -> bool:
        expression_verdict = _eval_expr_from_state(pair[1], trial.snap)
        if expression_verdict is not None:
            return expression_verdict
        verdict = constraint_holds(pair[1], trial.snap)
        return verdict is True or (
            verdict is None and _values_match(trial.snap.get(pair[0]), pair[1])
        )

    advanced_frontier = (
        tuple(pair for pair in frontier_pairs(frame.tree, frame.snap) if _frontier_holds(pair))
        if hasattr(frame.tree, "iter_nodes")
        else ()
    )
    target_advanced = bool(advanced_frontier)
    temporal_advanced = (
        accept_temporal_progress
        and target_advanced
        and all(
            constraint_holds(requirement.condition, trial.snap) is True
            for requirement in getattr(ctx, "temporal_requirements", ())
        )
    )
    # A bounded pulse needs no invented settle phase when its retained S1/S2
    # edge has already reduced the current target trace.  This is the common
    # scan-level conductivity receipt: the exact transaction did useful work,
    # even when the newly conductive owner exposes no scalar action frontier
    # until Compass reads the landing again.
    accept_override = accept_override or target_advanced
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
            active_requirements=tuple(getattr(state, "active_requirements", ())),
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
                    "channel_reached": channel_reached,
                    "channel_moved": channel_moved,
                    "trend_before": frame.distance_before,
                    "trend_after": new_trend,
                },
            )
            return None
        if target_advanced:
            _record_gate(
                "SCAN-PRODUCTIVE-STEP",
                ": exact scan edge improved target-relative distance",
                gate_events,
                evidence={
                    "trend_before": frame.distance_before,
                    "trend_after": new_trend,
                    "temporal": temporal_advanced,
                    "advanced_frontier": advanced_frontier,
                },
            )
        else:
            _record_gate(
                "CHANNEL-OVERRIDE-DEAD-END",
                ": channel target reached" if channel_reached else ": channel ejected",
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
        if earned_work_is_useful_motion(earned_work_receipt):
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
                    "channel_reached": channel_reached,
                    "channel_moved": channel_moved,
                },
            )
            return None
        else:
            _record_gate(
                "CHANNEL-OVERRIDE-LATERAL",
                ": channel target reached" if channel_reached else ": channel ejected",
                gate_events,
            )

    genuinely_new_actions = bool(new_actions - applied_inputs - old_actions)
    old_unsat = frame.tree.unsatisfied_conditions()
    new_unsat = new_tree.unsatisfied_conditions()
    genuinely_new_conditions = bool(new_unsat - old_unsat)
    has_new_frontier = genuinely_new_actions or genuinely_new_conditions or target_advanced
    return _DeadEndResult(
        tree=new_tree,
        trend=new_trend,
        has_new_frontier=has_new_frontier,
        advanced_frontier=advanced_frontier,
    )
