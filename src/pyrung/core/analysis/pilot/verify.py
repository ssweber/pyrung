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
    _apply_pulse,
    _DebugFn,
    _has_pending_effects,
    _install_holds,
    _pilot_state_key,
    _settle_delayed_effects,
    _StateKeyConfig,
)
from pyrung.core.analysis.pilot.investigate import chase_cause_roots
from pyrung.core.analysis.pilot.outcome import (
    Outcome,
    _has_compass_frontier,
    classify_outcome,
)
from pyrung.core.analysis.pilot.trace import trace_back
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.runner import PLC

logger = logging.getLogger(__name__)

_ActionPair = tuple[str, Any]
_StateKey = tuple[Any, ...]


# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PilotGateEvent:
    """Structured result from one candidate acceptance gate."""

    event: str
    detail: str = ""


@dataclass
class _PulseState:
    fork: PLC
    scan_before: int
    action_scan: int
    action_snap: dict[str, Any]
    wait_snaps: tuple[dict[str, Any], ...]
    post_pulse_snap: dict[str, Any]
    post_pulse_key: _StateKey
    snap: dict[str, Any]
    key: _StateKey


@dataclass(frozen=True)
class _DeadEndResult:
    tree: Any
    trend: int
    has_new_frontier: bool = False


@dataclass(frozen=True)
class _TrialResult:
    fork: PLC
    scan_before: int
    action: dict[str, Any]
    pulse_actions: tuple[_ActionPair, ...]
    before_snap: dict[str, Any]
    post_pulse_snap: dict[str, Any]
    fork_snap: dict[str, Any]
    observe_label: str
    new_key: _StateKey | None = None
    trend: int | None = None
    outcome: Outcome | None = None
    regression_nogoods: frozenset[_ActionPair] = frozenset()
    chase_regression_causes: bool = True
    gate_events: tuple[PilotGateEvent, ...] = ()


@dataclass(frozen=True)
class _AttemptResult:
    trial: _TrialResult | None
    gate_events: tuple[PilotGateEvent, ...] = ()


# ---------------------------------------------------------------------------
# Gate helpers — excursion diagnosis and retry
# ---------------------------------------------------------------------------


def _diagnose_excursion(
    fork: PLC,
    pre_snap: dict[str, Any],
    post_pulse_snap: dict[str, Any],
    cfg: _StateKeyConfig,
    steerable: frozenset[str],
) -> tuple[list[str], list[tuple[str, Any]]]:
    """Find reverted state-key dimensions and chase cause to derive holds.

    Called when the post-settle key matches the pre-action key but the
    post-pulse key was different (excursion detected).  The fork's most
    recent transition for each reverted tag IS the revert transition, so
    ``chase_cause_roots`` traces the right chain.

    In addition to trigger-based holds (from ``chase_cause_roots``), this
    scans the cause chain's step enablers for steerable inputs.  Holding a
    Bool enabler at its negated value prevents the clearing rung from
    firing on retry.

    Returns ``(reverted_tags, holds)``.
    """
    reverted: list[str] = []
    for i, name in enumerate(cfg.stateful_names):
        if i in cfg.acc_indices:
            continue
        pre_val = pre_snap.get(name)
        pulse_val = post_pulse_snap.get(name)
        if not _values_match(pre_val, pulse_val):
            reverted.append(name)

    all_holds: list[tuple[str, Any]] = []
    seen_holds: set[tuple[str, Any]] = set()
    for tag in reverted:
        _, holds = chase_cause_roots(fork, tag, steerable)
        for h in holds:
            if h not in seen_holds:
                seen_holds.add(h)
                all_holds.append(h)

        try:
            chain = fork.cause(tag)
        except Exception:  # noqa: BLE001
            continue
        if chain is None:
            continue
        for step in chain.steps:
            for enabler in step.enablers:
                if enabler.tag_name not in steerable:
                    continue
                if not isinstance(enabler.value, bool):
                    continue
                hold = (enabler.tag_name, not enabler.value)
                if hold not in seen_holds:
                    seen_holds.add(hold)
                    all_holds.append(hold)

    return reverted, all_holds


def _attempt_excursion_retry(
    work: PLC,
    action: list[tuple[str, Any]],
    pre_snap: dict[str, Any],
    pre_key: tuple[Any, ...],
    excursion_holds: list[tuple[str, Any]],
    forced_holds: dict[str, Any],
    resting: dict[str, Any],
    edge_tags: set[str],
    cfg: _StateKeyConfig,
    scan_budget: int,
) -> PLC | None:
    """Retry *action* with excursion-derived holds installed.

    Returns the retry fork if the state key held (differs from
    *pre_key*), otherwise ``None``.
    """
    retry = work.fork()
    combined: dict[str, Any] = {}
    _install_holds(retry, list(forced_holds.items()), combined)
    _install_holds(retry, excursion_holds, combined)
    _apply_pulse(retry, action, resting, edge_tags)
    _settle_delayed_effects(retry, pre_snap, cfg, scan_budget=scan_budget)
    retry_snap = dict(retry.state.tags)
    retry_key = _pilot_state_key(retry_snap, cfg)
    if retry_key != pre_key:
        return retry
    return None


def _detect_latched_side_effects(
    pre_snap: dict[str, Any],
    post_snap: dict[str, Any],
    cfg: _StateKeyConfig,
) -> dict[str, Any]:
    """Tags outside the state key that changed during an excursion and stuck."""
    key_tags = set(cfg.stateful_names)
    latched: dict[str, Any] = {}
    for tag, new_val in post_snap.items():
        if tag in key_tags:
            continue
        old_val = pre_snap.get(tag)
        if not _values_match(old_val, new_val):
            latched[tag] = new_val
    return latched


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
        reverted, exc_holds = _diagnose_excursion(
            trial.fork,
            frame.snap,
            trial.post_pulse_snap,
            key_config,
            ctx.steerable,
        )
        action_tags = {t for t, _ in action_pairs}
        useful_holds = [(h, hv) for h, hv in exc_holds if h not in action_tags]
        if useful_holds:
            retry = _attempt_excursion_retry(
                state.work,
                list(action_pairs),
                frame.snap,
                frame.key,
                useful_holds,
                state.forced_holds,
                ctx.resting,
                ctx.edge_tags,
                key_config,
                ctx.max_scans - state.work.state.scan_id,
            )
            if retry is not None:
                _install_holds(state.work, useful_holds, state.forced_holds)
                retry_snap = dict(retry.state.tags)
                retry_key = _pilot_state_key(retry_snap, key_config)
                _gate_debug(
                    dbg,
                    debug_name,
                    "EXCURSION-RETRY-OK",
                    f": reverted={reverted}, holds={useful_holds}",
                    gate_events,
                )
                return _PulseState(
                    fork=retry,
                    scan_before=trial.scan_before,
                    action_scan=trial.action_scan,
                    action_snap=trial.action_snap,
                    wait_snaps=trial.wait_snaps,
                    post_pulse_snap=trial.post_pulse_snap,
                    post_pulse_key=trial.post_pulse_key,
                    snap=retry_snap,
                    key=retry_key,
                )
            _gate_debug(dbg, debug_name, "EXCURSION-RETRY-FAIL", gate_events=gate_events)
            return None

        side_effects = _detect_latched_side_effects(frame.snap, trial.snap, key_config)
        if side_effects:
            _gate_debug(
                dbg,
                debug_name,
                "EXCURSION-SIDE-EFFECTS",
                f": {list(side_effects)[:5]}",
                gate_events,
            )
        _gate_debug(dbg, debug_name, "EXCURSION-NO-HOLDS", gate_events=gate_events)
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
        ),
        gate_events=tuple(gate_events),
    )
