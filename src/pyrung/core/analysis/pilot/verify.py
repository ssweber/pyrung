"""Verify — gate pipeline for PILOT trial acceptance.

After an Act (command pulse or zoom), the trial flows through:

  0. Avoid gate     — settled state matches avoid predicate
  1. Target gate    — target already reached (early exit)
  2. Spin gate      — state key must change (with excursion retry)
  3. Cycle gate     — new key must not have been visited
  4. Dead-end gate  — frontier must be non-empty or async pending
  5. Outcome        — classify via ``outcome.py``

Both ``_try_action_batch`` and ``_try_zoom`` converge on ``verify_gates``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _DebugFn,
    _has_pending_effects,
    _install_holds,
    _pilot_state_key,
)
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.investigate import investigate_excursion
from pyrung.core.analysis.pilot.outcome import (
    Outcome,
    _has_compass_frontier,
    classify_outcome,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.pilot.types import (
    PilotGateEvent,
    _ActionPair,
    _AttemptResult,
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


# ---------------------------------------------------------------------------
# Gate helpers — excursion diagnosis and retry
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Gate functions
# ---------------------------------------------------------------------------


def _gate_debug(
    dbg: _DebugFn,
    name: str,
    event: str,
    detail: str = "",
    gate_events: list[PilotGateEvent] | None = None,
) -> None:
    if gate_events is not None:
        gate_events.append(PilotGateEvent(event=event.lower(), detail=detail.lstrip(": ")))
    if name.startswith("WIDTH-"):
        dbg(f"# {name}-{event}{detail}")
    else:
        dbg(f"#     {event} {name}{detail}")


def _gate_spin(
    trial: _PulseState,
    action_pairs: tuple[_ActionPair, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    dbg: _DebugFn,
    *,
    debug_name: str,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
) -> _PulseState | None:
    key_config = state.key_config
    assert key_config is not None

    if trial.key != frame.key or _has_pending_effects(trial.fork):
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
            forced_holds=state.forced_holds,
            resting=ctx.resting,
            edge_tags=ctx.edge_tags,
            scan_budget=ctx.max_scans - state.work.state.scan_id,
        )
        if result.retry_fork is not None:
            _install_holds(state.work, result.confirmed_holds, state.forced_holds)
            retry_snap = dict(result.retry_fork.state.tags)
            retry_key = _pilot_state_key(retry_snap, key_config)
            _gate_debug(
                dbg,
                debug_name,
                "EXCURSION-RETRY-OK",
                f": reverted={result.reverted}, holds={result.confirmed_holds}",
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
            )
        if result.reverted:
            _gate_debug(dbg, debug_name, "EXCURSION-NO-HOLDS", gate_events=gate_events)
        else:
            _gate_debug(dbg, debug_name, "EXCURSION-RETRY-FAIL", gate_events=gate_events)
        return None

    if nogood_pair is not None:
        state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
    _gate_debug(dbg, debug_name, "SPIN", gate_events=gate_events)
    return None


def _gate_cycle(
    trial: _PulseState,
    frame: Any,
    state: Any,
    dbg: _DebugFn,
    *,
    pending: bool,
    influence_prescribed: bool,
    debug_name: str,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
) -> bool:
    if trial.key not in state.seen_keys or pending:
        return True
    if not influence_prescribed:
        if nogood_pair is not None:
            state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
        _gate_debug(dbg, debug_name, "CYCLE", gate_events=gate_events)
        return False
    _gate_debug(
        dbg,
        debug_name,
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
    dbg: _DebugFn,
    *,
    influence_prescribed: bool,
    debug_name: str,
    nogood_pair: _ActionPair | None,
    gate_events: list[PilotGateEvent],
) -> _DeadEndResult | None:
    new_tree = trace_back(
        ctx.target_tag,
        ctx.target_value,
        trial.snap,
        ctx.pdg,
        ctx.program,
        ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        pipeline_internal_tags=ctx.pipeline_internal_tags,
        choice=ctx.choice,
    )
    new_trend = new_tree.unsatisfied_count()
    new_actions = set(new_tree.ordered_actions())
    old_actions = set(frame.tree.ordered_actions())
    action_inputs = set(action_pairs)
    influence_frontier = _has_compass_frontier(new_tree, trial.snap, ctx.opaque_loop)
    pending = _has_pending_effects(trial.fork)

    if not new_actions and not influence_frontier and not pending:
        if not influence_prescribed:
            if nogood_pair is not None:
                state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
            _gate_debug(
                dbg,
                debug_name,
                "DEAD-END",
                ": empty frontier, no pending effects",
                gate_events,
            )
            return None
        _gate_debug(
            dbg,
            debug_name,
            "INFLUENCE-OVERRIDE-DEAD-END",
            ": influence-prescribed",
            gate_events,
        )
    elif (
        new_actions
        and not (new_actions - action_inputs - old_actions)
        and new_trend >= frame.distance_before
    ):
        if not influence_prescribed:
            if nogood_pair is not None:
                state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
            _gate_debug(
                dbg,
                debug_name,
                "LATERAL",
                ": no new frontier, no trend improvement",
                gate_events,
            )
            return None
        _gate_debug(
            dbg,
            debug_name,
            "INFLUENCE-OVERRIDE-LATERAL",
            ": influence-prescribed",
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
    trial: _PulseState,
    action_pairs: tuple[_ActionPair, ...],
    pulse_actions: tuple[_ActionPair, ...],
    frame: Any,
    state: Any,
    ctx: Any,
    dbg: _DebugFn,
    *,
    observe_label: str,
    target_observe_label: str,
    debug_name: str,
    influence_prescribed: bool,
    route_prescribed: bool,
    nogood_pair: _ActionPair | None,
    regression_nogoods: frozenset[_ActionPair],
    chase_regression_causes: bool,
    zoom_governing_tag: str | None = None,
    zoom_target_value: Any = None,
) -> _AttemptResult:
    """Shared verify pipeline for both command pulses and zoom.

    Runs target check → spin gate → cycle gate → dead-end gate → outcome
    classification.  Both ``_try_action_batch`` and ``_try_zoom`` converge here.
    """
    gate_events: list[PilotGateEvent] = []

    if ctx.avoid_pred is not None and ctx.avoid_pred(trial.snap):
        gate_events.append(PilotGateEvent("avoid", "settled state matches avoid condition"))
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    if _values_match(trial.snap.get(ctx.target_tag), ctx.target_value):
        gate_events.append(PilotGateEvent("target", f"{ctx.target_tag}={ctx.target_value!r}"))
        return _AttemptResult(
            trial=_TrialResult(
                fork=trial.fork,
                scan_before=trial.scan_before,
                action=dict(action_pairs),
                pulse_actions=pulse_actions,
                before_snap=frame.snap,
                post_pulse_snap=trial.post_pulse_snap,
                fork_snap=trial.snap,
                observe_label=target_observe_label,
                regression_nogoods=regression_nogoods,
                chase_regression_causes=chase_regression_causes,
                gate_events=tuple(gate_events),
                zoom_governing_tag=zoom_governing_tag,
                zoom_target_value=zoom_target_value,
            ),
            gate_events=tuple(gate_events),
        )

    spun = _gate_spin(
        trial,
        action_pairs,
        frame,
        state,
        ctx,
        dbg,
        debug_name=debug_name,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
    )
    if spun is None:
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))
    trial = spun

    if _values_match(trial.snap.get(ctx.target_tag), ctx.target_value):
        gate_events.append(PilotGateEvent("target", f"{ctx.target_tag}={ctx.target_value!r}"))
        return _AttemptResult(
            trial=_TrialResult(
                fork=trial.fork,
                scan_before=trial.scan_before,
                action=dict(action_pairs),
                pulse_actions=pulse_actions,
                before_snap=frame.snap,
                post_pulse_snap=trial.post_pulse_snap,
                fork_snap=trial.snap,
                observe_label=target_observe_label,
                regression_nogoods=regression_nogoods,
                chase_regression_causes=chase_regression_causes,
                gate_events=tuple(gate_events),
                zoom_governing_tag=zoom_governing_tag,
                zoom_target_value=zoom_target_value,
            ),
            gate_events=tuple(gate_events),
        )

    pending = _has_pending_effects(trial.fork)
    if not _gate_cycle(
        trial,
        frame,
        state,
        dbg,
        pending=pending,
        influence_prescribed=influence_prescribed,
        debug_name=debug_name,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
    ):
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    dead_end = _gate_dead_end(
        trial,
        action_pairs,
        frame,
        state,
        ctx,
        dbg,
        influence_prescribed=influence_prescribed,
        debug_name=debug_name,
        nogood_pair=nogood_pair,
        gate_events=gate_events,
    )
    if dead_end is None:
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    outcome = classify_outcome(
        trial,
        action_pairs,
        frame,
        ctx,
        dead_end.trend,
        dead_end.has_new_frontier,
        chase_cause_roots,
        route_prescribed=route_prescribed,
        zoom_governing_tag=zoom_governing_tag,
        zoom_target_value=zoom_target_value,
    )

    if outcome == Outcome.BAD_EDGE:
        if nogood_pair is not None:
            state.nogoods.setdefault(frame.key, set()).add(nogood_pair)
        _gate_debug(
            dbg,
            debug_name,
            "BAD-EDGE",
            f": distance {frame.distance_before} -> {dead_end.trend}",
            gate_events,
        )
        return _AttemptResult(trial=None, gate_events=tuple(gate_events))

    outcome_tag = outcome.value.upper()
    if debug_name.startswith("WIDTH-"):
        dbg(f"# {debug_name}-{outcome_tag}: distance {frame.distance_before} -> {dead_end.trend}")
    else:
        dbg(
            f"#     {outcome_tag} {debug_name}: "
            f"distance {frame.distance_before} -> {dead_end.trend}"
        )
    gate_events.append(
        PilotGateEvent(outcome.value, f"distance {frame.distance_before} -> {dead_end.trend}")
    )

    return _AttemptResult(
        trial=_TrialResult(
            fork=trial.fork,
            scan_before=trial.scan_before,
            action=dict(action_pairs),
            pulse_actions=pulse_actions,
            before_snap=frame.snap,
            post_pulse_snap=trial.post_pulse_snap,
            fork_snap=trial.snap,
            observe_label=observe_label,
            new_key=trial.key,
            trend=dead_end.trend,
            outcome=outcome,
            regression_nogoods=regression_nogoods,
            chase_regression_causes=chase_regression_causes,
            gate_events=tuple(gate_events),
            zoom_governing_tag=zoom_governing_tag,
            zoom_target_value=zoom_target_value,
        ),
        gate_events=tuple(gate_events),
    )
