"""Execute and verify one proposed PILOT action or wait.

The `_try_*` functions prepare a fork, settle prerequisite regions, pulse an
action or coast through a dwell, and pass the resulting trial to
``verify.verify_gates``. They return an ``_AttemptResult`` containing the
verdict, receipts, and transition observations.

This module does not apply observations, replace the committed world, manage
checkpoints, or install corrections.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _ZOOM_BUDGET,
    PilotRung,
    _append_rungs,
    _avoid_violations,
    _coast_holding_state,
    _coast_to_value,
    _has_pending_effects,
    _pilot_world_key,
    _rung_identity,
    _settle_delayed_effects,
    coast_departure_tags,
    fork_with_rungs,
    wait_edge_nogood,
)
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.coast import LIMITS, CoastSession
from pyrung.core.analysis.pilot.compass import WAIT, Action, CompassObservation, is_action
from pyrung.core.analysis.pilot.navigation import (
    BatchPulse,
    Bearing,
    Coast,
    Dwell,
    OrientationWorld,
    Pulse,
)
from pyrung.core.analysis.pilot.trace import _all_nodes, target_reached
from pyrung.core.analysis.pilot.types import (
    MotionKind,
    PilotGateEvent,
    _ActionPair,
    _AttemptResult,
    _HoldLogEntry,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _PulseState,
)
from pyrung.core.analysis.pilot.verify import verify_gates
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.compass import TransitionCause
    from pyrung.core.runner import PLC


# ---------------------------------------------------------------------------
# Cone settlement — dwell control
# ---------------------------------------------------------------------------

_SETTLE_CONE_CEILING = LIMITS.cone_ceiling
_LETRUN_DWELL_CEILING = LIMITS.dwell_ceiling


class StaleBearingError(RuntimeError):
    """The world changed after orientation and before execution."""


def _install_prerequisites(
    state: _PilotState, prerequisites: tuple[PilotRung, ...]
) -> None:
    """Install only prerequisite rungs that do not already have an owner."""
    existing = {_rung_identity(rung) for rung in state.rungs}
    new_rungs = tuple(rung for rung in prerequisites if _rung_identity(rung) not in existing)
    if not new_rungs:
        return
    state.rungs = _append_rungs(state.work, list(new_rungs), state.rungs)
    state.hold_log.append(
        _HoldLogEntry(
            scan=state.work.state.scan_id,
            source="prerequisite",
            rungs=new_rungs,
        )
    )


def _settle_cone(
    fork: PLC,
    cone: frozenset[str],
    *,
    floor: int = LIMITS.cone_floor,
    ceiling: int = _SETTLE_CONE_CEILING,
    reached_fn: Callable[[dict[str, Any]], bool] | None = None,
    session: CoastSession | None = None,
) -> list[dict[str, Any]]:
    """Coast *fork* until the cone stops moving — dwell control only.

    Thin wrapper over :meth:`CoastSession.settle` (see its docstring for the
    fixpoint/floor/transient semantics); returns the per-scan trajectory.
    *session*, when given, records the dwell onto that session's timeline.

    Settle never accepts or rejects.  Attributing the trajectory to one of the
    five verify outcomes — who moved what — is the caller's job via ``cause()``.
    """
    if session is None:
        session = CoastSession(fork, kind="settle")
    assert session.plc is fork
    receipt = session.settle(cone, floor=floor, ceiling=ceiling, reached_fn=reached_fn)
    return list(receipt.trajectory)


def _pen_tags(state: _PilotState, ctx: _PilotContext) -> frozenset[str]:
    """The trial recorder's pen universe.

    Profile Done bits (a fire-then-reset watchdog pulse must be two recorded
    transitions, not a net no-op) plus the loop's watch tags and pipeline role
    channels (bearing departures land as recorded events with exact scans).
    Accumulator registers are excluded: a change-pen on a per-scan-churny tag
    would collapse every fold to step-mode, and acc membership in the
    incident's changed set is served by endpoint diff instead.
    """
    from pyrung.core.analysis.pilot.advance import iter_advance_owners

    dones: set[str] = set()
    accs: set[str] = set()
    if ctx.program is not None:
        for owner in iter_advance_owners(ctx.program):
            profile = owner.profile
            if profile.done is not None:
                dones.add(profile.done.name)
            if profile.accumulator is not None:
                accs.add(profile.accumulator.name)
    tags = dones | set(state.watch_tags) | {r.channel_tag for r in ctx.pipeline_roles}
    return frozenset(tags - accs)


def _cone_tags(frame: _IterationFrame, ctx: _PilotContext) -> frozenset[str]:
    """The tags whose motion matters this iteration.

    The trace-tree prerequisites toward the goal — satisfied *and* unsatisfied,
    so a prerequisite slipping back (divergence) is visible, not just one being
    met — plus the channel / opaque-loop registers.  Steerable inputs are
    excluded: those are held, not watched.
    """
    tags = {n.tag for n in _all_nodes(frame.tree) if not n.is_steerable}
    return frozenset(tags | ctx.opaque_loop)


# ---------------------------------------------------------------------------
# Pulse execution
# ---------------------------------------------------------------------------


def _apply_actions(
    actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _PulseState:
    key_config = state.key_config
    assert key_config is not None

    fork = fork_with_rungs(state.work, state.rungs)
    scan_before = fork.state.scan_id
    session = CoastSession(fork, kind="pulse")
    session.arm_pens(_pen_tags(state, ctx))
    patch = {t: v for t, v in actions}
    needs_edge = any(t in ctx.edge_tags for t in patch)

    if needs_edge:
        release = {t: ctx.resting.get(t, False) for t in patch if t in ctx.edge_tags}
        if release:
            fork.patch(release)
            fork.step()
            session.note_pens()

    fork.patch(patch)
    fork.step()
    session.note_pens()
    action_snap = dict(fork.state.tags)
    action_scan = fork.state.scan_id

    # Stop the settle the scan the target holds — otherwise the cone-fixpoint
    # coast (and the delayed-effect fast-forward) steps straight through a
    # one-scan transient (STARTING → EXECUTE) and the post-settle check never
    # sees it.  Landing the fork on the transient lets verify confirm it.
    def _reached(tags: dict[str, Any]) -> bool:
        return target_reached(tags, ctx.target_tag, ctx.target_value, ctx.target_predicate)

    if _reached(action_snap):
        wait_snaps: list[dict[str, Any]] = []
    else:
        wait_snaps = _settle_cone(
            fork, _cone_tags(frame, ctx), floor=2, reached_fn=_reached, session=session
        )

    post_pulse_snap = dict(fork.state.tags)
    post_pulse_key = _pilot_world_key(post_pulse_snap, key_config, state.rungs)
    if not _reached(post_pulse_snap):
        _settle_delayed_effects(
            fork,
            frame.snap,
            key_config,
            scan_budget=ctx.max_scans - fork.state.scan_id,
            session=session,
        )
    fork_snap = dict(fork.state.tags)
    if wait_snaps and wait_snaps[-1] != fork_snap:
        wait_snaps.append(fork_snap)
    elif not wait_snaps and action_snap != fork_snap:
        wait_snaps.append(fork_snap)
    return _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=action_scan,
        action_snap=action_snap,
        wait_snaps=tuple(wait_snaps),
        post_pulse_snap=post_pulse_snap,
        post_pulse_key=post_pulse_key,
        snap=fork_snap,
        key=_pilot_world_key(fork_snap, key_config, state.rungs),
        timeline=session.events,
    )


# ---------------------------------------------------------------------------
# Compass observation gathering — execution observes; the drive loop applies
# ---------------------------------------------------------------------------


def _action_caused_change(
    fork: PLC,
    action_tag: str,
    changed_tag: str,
    steerable: frozenset[str],
    *,
    scan: int | None,
) -> bool:
    """True if *action_tag* is a causal root of *changed_tag*'s transition.

    Distinguishes a change the pilot's control input produced from one that
    happened ambiently in the same scan (a timer or alarm firing).  This is the
    "control vs wind" check: only the former should be learned as an action
    transition.
    """
    roots, _holds = chase_cause_roots(fork, changed_tag, steerable, scan=scan)
    return action_tag in roots


def _compass_observations(
    cause: TransitionCause,
    frame: _IterationFrame,
    before_snap: dict[str, Any],
    after_snap: dict[str, Any],
    ctx: _PilotContext,
    *,
    contradict_no_change: bool,
    fork: PLC | None = None,
    scan: int | None = None,
) -> tuple[CompassObservation, ...]:
    """Return compass-relevant motion between two snapshots without applying it.

    The causal chase is evidence gathering. The drive loop later applies the
    returned observations to its persistent compass value.
    """
    action_tag = cause[0] if is_action(cause) else None
    observations: list[CompassObservation] = []
    for n in _all_nodes(frame.tree):
        # pipeline_internal nodes are included: the learned table is the
        # pipeline instrument's own memory, and a live trial is the strongest
        # evidence there is — both for new edges and for falsifying stale
        # static-catalog ones.
        if n.satisfied or n.is_steerable:
            continue
        old_v = before_snap.get(n.tag)
        new_v = after_snap.get(n.tag)
        if old_v != new_v and new_v is not None:
            if (
                action_tag is not None
                and fork is not None
                and not _action_caused_change(fork, action_tag, n.tag, ctx.steerable, scan=scan)
            ):
                continue
            observations.append(CompassObservation("edge", n.tag, cause, old_v, new_v))
        elif contradict_no_change:
            # The cause fired from old_v under a full settle window and the
            # register did not move — falsify any learned edge claiming it
            # would (a static-catalog route ignores unreadable enablers),
            # and mark the probe so it is not re-sent.
            observations.append(CompassObservation("contradict", n.tag, cause, old_v))
    return tuple(observations)


# ---------------------------------------------------------------------------
# Try-verify wrappers
# ---------------------------------------------------------------------------


def _try_action_batch(
    action_pairs: tuple[_ActionPair, ...],
    applied: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    observe_label: str,
    target_observe_label: str,
    influence_prescribed: bool,
    route_prescribed: bool,
    nogood_pair: _ActionPair | None,
    regression_nogoods: frozenset[_ActionPair],
    chase_regression_causes: bool,
    record_influence_action: Action | None = None,
    bearing_channel_tag: str | None = None,
    bearing_channel_value: Any = None,
) -> _AttemptResult:
    # ── Action gate (avoid=) ──────────────────────────────────────────────
    # Before the pulse: a candidate whose overlaid action makes the avoid
    # predicate true is a path that depends on the avoided condition — reject it
    # *without* pressing, so a momentary command (avoid=C_Complete) is never
    # pulsed.  Static: overlay the applied set onto the live snapshot and read
    # the predicate.  nogood the choice so the next iteration stops surfacing it
    # (candidates filters nogoods), and record the names so the terminal decline
    # can point at what excluded the path.
    avoid_names = _avoid_violations(ctx, applied, frame.snap)
    if avoid_names:
        return _AttemptResult(
            trial=None,
            gate_events=(
                PilotGateEvent("avoid", f"action would enter avoid: {', '.join(avoid_names)}"),
            ),
            nogood_pairs=frozenset({nogood_pair}) if nogood_pair is not None else frozenset(),
            avoid_names=tuple(avoid_names),
        )

    trial = _apply_actions(applied, frame, state, ctx)

    observations: list[CompassObservation] = []
    if record_influence_action is not None:
        observations.extend(
            _compass_observations(
                record_influence_action,
                frame,
                frame.snap,
                trial.action_snap,
                ctx,
                contradict_no_change=True,
                fork=trial.fork,
                scan=trial.action_scan,
            )
        )
    wait_before = trial.action_snap
    for wait_after in trial.wait_snaps:
        observations.extend(
            _compass_observations(
                WAIT,
                frame,
                wait_before,
                wait_after,
                ctx,
                contradict_no_change=False,
            )
        )
        wait_before = wait_after

    result = verify_gates(
        trial,
        action_pairs,
        applied,
        frame,
        state,
        ctx,
        observe_label=observe_label,
        target_observe_label=target_observe_label,
        influence_prescribed=influence_prescribed,
        route_prescribed=route_prescribed,
        nogood_pair=nogood_pair,
        regression_nogoods=regression_nogoods,
        chase_regression_causes=chase_regression_causes,
        zoom_channel_tag=bearing_channel_tag,
        zoom_target_value=bearing_channel_value,
    )
    return replace(result, observations=tuple(observations))


def execute(bearing: Bearing, world: OrientationWorld) -> _AttemptResult:
    """Execute exactly the act declared by a current-world bearing.

    This is deliberately narrower than orientation: it validates the world
    binding, installs declared prerequisites, and dispatches one act through
    the existing verification pipeline.  It never selects a fallback.
    """

    frame = world.frame
    state = world.state
    ctx = world.context
    key_config = state.key_config
    if key_config is None:
        raise StaleBearingError("cannot execute a bearing before the world key is configured")
    live_key = _pilot_world_key(dict(state.work.state.tags), key_config, state.rungs)
    if live_key != bearing.world_key:
        raise StaleBearingError(
            f"bearing world {bearing.world_key!r} is stale; current world is {live_key!r}"
        )

    if bearing.prerequisites:
        _install_prerequisites(state, tuple(bearing.prerequisites))

    act = bearing.act
    if isinstance(act, Pulse):
        option = act.option
        return _try_action_batch(
            (act.action,),
            act.applied,
            frame,
            state,
            ctx,
            observe_label="accept",
            target_observe_label="target",
            influence_prescribed=option.influence_prescribed,
            # A structural program-owned current is also an explicit bearing:
            # if it opens a new frontier, commit it so progress monitoring can
            # investigate and learn the corrective holds.  It is not a static
            # route suffix and never bypasses the live avoid gate.
            route_prescribed=option.route_prescribed or option.current_prescribed,
            nogood_pair=act.action,
            regression_nogoods=frozenset({act.action}),
            chase_regression_causes=True,
            record_influence_action=act.action,
            bearing_channel_tag=option.bearing_channel_tag,
            bearing_channel_value=option.bearing_channel_value,
        )
    if isinstance(act, BatchPulse):
        return _try_action_batch(
            act.actions,
            act.actions,
            frame,
            state,
            ctx,
            observe_label="batch" if act.source == "learned" else "width",
            target_observe_label=("batch-target" if act.source == "learned" else "width-target"),
            influence_prescribed=act.source == "learned",
            route_prescribed=False,
            nogood_pair=None,
            regression_nogoods=frozenset(act.actions),
            chase_regression_causes=False,
        )
    if isinstance(act, Coast):
        if act.mode == "bearing":
            return _try_zoom(
                act.channel_tag,
                act.target_value,
                act.route_prescribed,
                frame,
                state,
                ctx,
                boundary=act.boundary,
                route_channel_tag=act.route_channel_tag,
                route_from_value=act.route_from_value,
                route_target_value=act.route_target_value,
            )
        return _try_terminal_letrun(frame, state, ctx)
    if isinstance(act, Dwell):
        return _try_terminal_dwell(frame, state, ctx)
    raise TypeError(f"unsupported navigation act {type(act).__name__}")


# ---------------------------------------------------------------------------
# Zoom — coast past timer/step-counter plateaus
# ---------------------------------------------------------------------------


def _try_zoom(
    channel_tag: str | None,
    target_value: Any,
    route_prescribed: bool,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    boundary: Any = None,
    route_channel_tag: str | None = None,
    route_from_value: Any = None,
    route_target_value: Any = None,
) -> _AttemptResult:
    """Let-run zoom through the verify pipeline — same shape as _try_candidate.

    Forks, zooms past timer/step-counter plateaus, then runs the shared
    verify gates.  The outcome classifier sees zoom results the same way it
    sees command results: SPIN if nothing moved, CONFIRMED if the channel
    register transitioned forward, AMBIENT_DRIFT if the program ejected.

    An ejection (e.g. S_StateCurrent 3→9) is AMBIENT_DRIFT with trend
    regression.  ``_monitor_trend`` reverts to the last checkpoint; a future
    investigation layer should own bounded incident analysis and replay-tested
    corrective holds.
    """
    fork = fork_with_rungs(state.work, state.rungs)
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)

    # A rejected wait is evidence about THIS world: the edge did not complete
    # here (a recipe-gated automatic transition, a dwell that never arms).
    # Record it as a world-keyed nogood so the next iteration's route query walks
    # around the edge instead of re-burning the same sterile coast.
    wait_channel = route_channel_tag or channel_tag
    wait_nogood = (
        wait_edge_nogood(
            wait_channel,
            route_from_value if route_channel_tag is not None else snap_before.get(channel_tag),
            route_target_value if route_channel_tag is not None else target_value,
        )
        if wait_channel is not None
        else None
    )

    # Confirmed conditional holds (oscillation correctives) animate during the
    # channel coast, same as the terminal let-run — fork_with_rungs installs
    # only the steady half.
    session = CoastSession(fork, kind="zoom")
    session.arm_pens(_pen_tags(state, ctx))
    dwell, zoom_receipt = _letrun_zoom(
        fork,
        channel_tag,
        target_value,
        cone=_cone_tags(frame, ctx),
        session=session,
        boundary=boundary,
        route_channel_tag=route_channel_tag,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(snap_after, key_config, state.rungs)

    observations: list[CompassObservation] = []
    wait_before = snap_before
    for wait_after in dwell:
        observations.extend(
            _compass_observations(
                WAIT,
                frame,
                wait_before,
                wait_after,
                ctx,
                contradict_no_change=False,
            )
        )
        wait_before = wait_after

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=tuple(dwell),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
        coast_receipt=zoom_receipt,
        timeline=session.events,
    )

    departed_route = (
        zoom_receipt is not None
        and zoom_receipt.stop_reason == "departed"
        and route_channel_tag is not None
    )
    verify_channel = route_channel_tag if departed_route else channel_tag
    verify_target = route_target_value if departed_route else target_value
    result = verify_gates(
        trial,
        action_pairs=(),
        applied=(),
        frame=frame,
        state=state,
        ctx=ctx,
        observe_label="zoom",
        target_observe_label="zoom-target",
        influence_prescribed=False,
        route_prescribed=route_prescribed,
        nogood_pair=wait_nogood,
        regression_nogoods=frozenset(),
        chase_regression_causes=True,
        zoom_channel_tag=verify_channel,
        zoom_target_value=verify_target,
        motion=MotionKind.COAST_TO_BEARING,
    )
    return replace(result, observations=tuple(observations))


def _try_terminal_letrun(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Generalized terminal let-run — the bottom-of-loop fallback.

    Reached here when no route zoom, no command candidate, and no widening made
    progress, yet the cone is still live (things pending).  The only move left is
    to hold the current macro-state and coast toward the global target, letting
    the program's self-advancing sub-processes (timers, step-counters) complete.

    Nothing about intermediate bearings is assumed: the heading is the global
    target, and the ejection guard is the recognized state-machine roles held at
    their current values.  Outcomes route through the shared verify pipeline:

      - target reached  -> CONFIRMED (the global-target check in verify_gates).
      - macro-state left -> AMBIENT_DRIFT; commit + _monitor_trend hands the
        ejection to investigation (the same path the doors took).
      - stall (budget, no target, no ejection) -> dead-end reject; the caller
        falls back to a bounded cone settle.
    """
    role_tags = coast_departure_tags(state, ctx)
    # fork_with_rungs re-establishes the steady holds on the coast fork: force
    # overrides do not propagate through fork(), and a freshly-installed
    # prerequisite — e.g. the Enable that drives a harness sensor's ramp — has not
    # been scanned onto state.work yet, so its value isn't carried either.
    fork = fork_with_rungs(state.work, state.rungs)
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)
    start_roles = {t: snap_before.get(t) for t in role_tags}

    # Confirmed conditional holds animate during the coast as oscillating rungs
    # in the holds overlay (cyclefold dispatch inside the coast session); they
    # are never forced steady.

    # A relational target (Temp >= 5.0) is reached when its predicate holds, not
    # when the register hits an exact value — coast on the predicate so a sensor
    # ramp driven by a held prerequisite (Enable) stops the moment it crosses.
    reached_fn = (
        (lambda s: target_reached(s.tags, ctx.target_tag, ctx.target_value, ctx.target_predicate))
        if ctx.target_predicate is not None
        else None
    )

    budget = min(_ZOOM_BUDGET, max(2, ctx.max_scans - scan_before))
    session = CoastSession(fork, kind="letrun")
    session.arm_pens(_pen_tags(state, ctx))
    letrun_receipt = _coast_holding_state(
        fork,
        ctx.target_tag,
        ctx.target_value,
        role_tags,
        budget=budget,
        reached_fn=reached_fn,
        session=session,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(snap_after, key_config, state.rungs)

    observations = _compass_observations(
        WAIT, frame, snap_before, snap_after, ctx, contradict_no_change=False
    )

    # Decide the outcome here — only the let-run knows the macro-state sentinel.
    #   reached  -> let the global-target check in verify_gates accept (CONFIRMED).
    #   ejected  -> a role left its held value: AMBIENT_DRIFT, handed to
    #               investigation via the changed role as the deviation bearing.
    #   stall    -> nothing reached, no role moved: a true dead end; let the
    #               caller fall back to a bounded cone settle.
    reached = target_reached(snap_after, ctx.target_tag, ctx.target_value, ctx.target_predicate)
    changed_channel = next(
        (t for t in role_tags if not _values_match(snap_after.get(t), start_roles[t])),
        None,
    )
    if not reached and changed_channel is None:
        # Hand the stall's receipt + pending flag to the loop: a quiescent
        # stall is trustworthy memo material (skip the re-coast at this world
        # key); a stall with a timer mid-flight must stay re-runnable.
        return _AttemptResult(
            trial=None,
            gate_events=(PilotGateEvent("dead-end", "terminal stall, no ejection"),),
            observations=observations,
            stall_receipt=letrun_receipt,
            stall_pending=_has_pending_effects(fork),
        )

    if reached:
        chan_tag: str | None = None
        chan_val: Any = None
    else:
        assert changed_channel is not None
        chan_tag = changed_channel
        chan_val = snap_before.get(changed_channel)

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=(snap_after,),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
        coast_receipt=letrun_receipt,
        timeline=session.events,
    )

    result = verify_gates(
        trial,
        action_pairs=(),
        applied=(),
        frame=frame,
        state=state,
        ctx=ctx,
        observe_label="letrun",
        target_observe_label="letrun-target",
        influence_prescribed=False,
        route_prescribed=False,
        nogood_pair=None,
        regression_nogoods=frozenset(),
        chase_regression_causes=True,
        zoom_channel_tag=chan_tag,
        zoom_target_value=chan_val,
        motion=MotionKind.COAST_HOLDING_WORLD,
    )
    return replace(result, observations=observations)


def _try_terminal_dwell(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Run one bounded repeated dwell through the shared trial gates.

    Reached only when Compass knowledge carries a terminal-coast receipt for
    this world key. The coast is deterministic under the held inputs,
    so repeating the full ejection-guarded let-run would reproduce the same
    departure.

    Perform one deterministic cone settle on a fork and route it through the
    same :func:`verify_gates` target gate as terminal let-run:

      - a self-advancing frontier that crosses the target during the dwell is
        CONFIRMED through the shared target gate (verify stays the sole source);
      - anything else is a legible terminal stall (dead-end reject), handed back
        to the caller's skiff / stuck exit.

    No ejection is committed and no investigation re-runs, so the loop cannot spin
    re-ejecting: a non-completing dwell terminates at the stuck exit rather than
    burning budget by coasting ``state.work`` to ``max_scans``.
    """
    fork = fork_with_rungs(state.work, state.rungs)
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)

    def _reached(tags: dict[str, Any]) -> bool:
        return target_reached(tags, ctx.target_tag, ctx.target_value, ctx.target_predicate)

    ceiling = min(_LETRUN_DWELL_CEILING, max(2, ctx.max_scans - scan_before))
    session = CoastSession(fork, kind="settle")
    session.arm_pens(_pen_tags(state, ctx))
    _settle_cone(
        fork, _cone_tags(frame, ctx), floor=2, ceiling=ceiling, reached_fn=_reached, session=session
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(snap_after, key_config, state.rungs)

    observations = _compass_observations(
        WAIT, frame, snap_before, snap_after, ctx, contradict_no_change=False
    )

    if not _reached(snap_after):
        # No new input is possible here and the cone quiesced without crossing the
        # target: a true terminal stall.  Do not classify a self-ejection as an
        # advance — return dead-end so the caller routes to the skiff / stuck exit.
        return _AttemptResult(
            trial=None,
            gate_events=(PilotGateEvent("dead-end", "terminal dwell settled short of target"),),
            observations=observations,
        )

    trial = _PulseState(
        fork=fork,
        scan_before=scan_before,
        action_scan=scan_before,
        action_snap=snap_before,
        wait_snaps=(snap_after,),
        post_pulse_snap=snap_before,
        post_pulse_key=frame.key,
        snap=snap_after,
        key=key_after,
        timeline=session.events,
    )

    # Empty actions, no channel register: the settled fork already reached the
    # target, so verify_gates accepts through its target gate (CONFIRMED).  Reuse
    # the "letrun" observe labels so commit folds the steady holds into the
    # recorded inputs the same way (the coast's driver is the held context).
    result = verify_gates(
        trial,
        action_pairs=(),
        applied=(),
        frame=frame,
        state=state,
        ctx=ctx,
        observe_label="letrun",
        target_observe_label="letrun-target",
        influence_prescribed=False,
        route_prescribed=False,
        nogood_pair=None,
        regression_nogoods=frozenset(),
        chase_regression_causes=True,
        zoom_channel_tag=None,
        zoom_target_value=None,
        motion=MotionKind.COAST_HOLDING_WORLD,
    )
    return replace(result, observations=observations)


def _letrun_zoom(
    work: PLC,
    channel_tag: str | None,
    target_value: Any,
    cone: frozenset[str],
    session: CoastSession | None = None,
    *,
    boundary: Any = None,
    route_channel_tag: str | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """Coast the live state past timer/step-counter plateaus.

    The zoom has its own generous budget (``_ZOOM_BUDGET``) — it does NOT
    consume the pilot's iteration budget.  Timer dwell is waiting, not
    searching.

    With a channel register and target value, seek with the target and
    departure bumps armed — the coast lands on the exact scan either fires
    and the returned receipt says which.  Without a channel register, fall
    back to the bounded single-step cone settle (no receipt — outcome's
    settle-path arm depends on its absence; the session still records pens).

    Returns ``(trajectory, receipt_or_None)``.
    """
    if channel_tag is None:
        return (
            _settle_cone(work, cone, floor=2, ceiling=_LETRUN_DWELL_CEILING, session=session),
            None,
        )

    budget = _ZOOM_BUDGET
    if boundary is not None:
        from pyrung.core.analysis.pilot.advance import build_advance_index
        from pyrung.core.instruction.advance import constraint_holds

        owner = build_advance_index(
            work.program,
            getattr(work, "_harness", None),
        ).resolve(getattr(boundary, "tag", ""))
        if owner is not None and owner.profile.linear is not None:
            estimate = owner.profile.linear.estimate_scans(
                boundary,
                work.state.tags,
                work._dt,
            )
            if estimate is not None:
                budget = max(budget, estimate + 2)
        receipt = _coast_holding_state(
            work,
            channel_tag,
            target_value,
            ((route_channel_tag,) if route_channel_tag is not None else ()),
            budget=budget,
            reached_fn=lambda state: constraint_holds(boundary, state.tags) is True,
            session=session,
        )
    else:
        receipt = _coast_to_value(
            work,
            channel_tag,
            target_value,
            budget=budget,
            session=session,
        )
    return [dict(work.state.tags)], receipt
