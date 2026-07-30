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

from pyrung.core.analysis.pilot.advance import estimate_owned_boundary_scans
from pyrung.core.analysis.pilot.avoid import _avoid_violations
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.coast import (
    _COAST_BUDGET,
    LIMITS,
    CoastReceipt,
    CoastSession,
    _coast_holding_state,
    _coast_to_value,
    _has_pending_effects,
    _settle_delayed_effects,
    coast_departure_tags,
)
from pyrung.core.analysis.pilot.compass import WAIT, Action, CompassObservation, is_action
from pyrung.core.analysis.pilot.navigation_contracts import (
    BatchPulse,
    Bearing,
    Coast,
    Dwell,
    OrientationWorld,
    Pulse,
)
from pyrung.core.analysis.pilot.overlay import (
    PilotRung,
    _append_rungs,
    _constraint_condition,
    fork_with_rungs,
)
from pyrung.core.analysis.pilot.trace import target_reached
from pyrung.core.analysis.pilot.types import (
    ChannelMotion,
    PilotGateEvent,
    _ActionPair,
    _AttemptResult,
    _ExecutedAttempt,
    _HoldLogEntry,
    _IterationFrame,
    _PilotContext,
    _PilotState,
    _PulseState,
)
from pyrung.core.analysis.pilot.verify import verify_gates
from pyrung.core.analysis.pilot.world_key import _pilot_world_key, _rung_identity
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


def _install_prerequisites(state: _PilotState, prerequisites: tuple[PilotRung, ...]) -> None:
    """Install only prerequisite rungs that do not already have an owner."""
    existing = {_rung_identity(rung) for rung in state.pilot_rungs}
    new_rungs = tuple(rung for rung in prerequisites if _rung_identity(rung) not in existing)
    if not new_rungs:
        return
    state.pilot_rungs = _append_rungs(
        state.work,
        list(new_rungs),
        state.pilot_rungs,
    )
    state.hold_log.append(
        _HoldLogEntry(
            scan=state.work.state.scan_id,
            source="prerequisite",
            rungs=new_rungs,
        )
    )


def _settle_watched_tags(
    fork: PLC,
    watched_tags: frozenset[str],
    *,
    floor: int = LIMITS.cone_floor,
    ceiling: int = _SETTLE_CONE_CEILING,
    reached_fn: Callable[[dict[str, Any]], bool] | None = None,
    session: CoastSession | None = None,
) -> list[dict[str, Any]]:
    """Coast *fork* until the watched tags stop moving — dwell control only.

    Thin wrapper over :meth:`CoastSession.settle` (see its docstring for the
    fixpoint/floor/transient semantics); returns the per-scan trajectory.
    *session*, when given, records the dwell onto that session's timeline.

    Settle never accepts or rejects.  Attributing the trajectory to one of the
    five verify outcomes — who moved what — is the caller's job via ``cause()``.
    """
    if session is None:
        session = CoastSession(fork, kind="settle")
    assert session.plc is fork
    receipt = session.settle(watched_tags, floor=floor, ceiling=ceiling, reached_fn=reached_fn)
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


def _watched_tags(frame: _IterationFrame, ctx: _PilotContext) -> frozenset[str]:
    """The tags whose motion matters this iteration.

    The trace-tree prerequisites toward the goal — satisfied *and* unsatisfied,
    so a prerequisite slipping back (divergence) is visible, not just one being
    met — plus the channel / opaque-loop registers.  Steerable inputs are
    excluded: those are held, not watched.
    """
    tags = {n.tag for n in frame.tree.iter_nodes() if not n.is_steerable}
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

    fork = fork_with_rungs(state.work, state.pilot_rungs)
    scan_before = fork.state.scan_id
    session = CoastSession(fork, kind="pulse")
    session.arm_avoid(ctx.avoid_pred)
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

    # Stop the settle the scan the target holds — otherwise the watched-tag fixpoint
    # coast (and the delayed-effect fast-forward) steps straight through a
    # one-scan transient (STARTING → EXECUTE) and the post-settle check never
    # sees it.  Landing the fork on the transient lets verify confirm it.
    def _reached(tags: dict[str, Any]) -> bool:
        return target_reached(tags, ctx.target.tag, ctx.target.value, ctx.target.predicate)

    if _reached(action_snap):
        wait_snaps: list[dict[str, Any]] = []
    else:
        wait_snaps = _settle_watched_tags(
            fork, _watched_tags(frame, ctx), floor=2, reached_fn=_reached, session=session
        )

    post_pulse_snap = dict(fork.state.tags)
    post_pulse_key = _pilot_world_key(post_pulse_snap, key_config, state.pilot_rungs)
    delayed_receipts: list[CoastReceipt] = []
    if not _reached(post_pulse_snap):
        delayed_receipts = _settle_delayed_effects(
            fork,
            scan_budget=state.remaining_search_scans(
                ctx.max_scans,
                scan_id=fork.state.scan_id,
            ),
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
        key=_pilot_world_key(fork_snap, key_config, state.pilot_rungs),
        coast_receipt=(delayed_receipts[-1] if delayed_receipts else None),
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
    world_key: tuple[Any, ...],
    applied: tuple[_ActionPair, ...] = (),
    fork: PLC | None = None,
    scan: int | None = None,
) -> tuple[CompassObservation, ...]:
    """Return compass-relevant motion between two snapshots without applying it.

    The causal chase is evidence gathering. The drive loop later applies the
    returned observations to its persistent compass value.
    """
    action_tag = cause[0] if is_action(cause) else None
    observations: list[CompassObservation] = []
    for n in frame.tree.iter_nodes():
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
            observations.append(
                CompassObservation(
                    "edge",
                    n.tag,
                    cause,
                    old_v,
                    new_v,
                    world_key,
                    tuple(sorted(before_snap.items())),
                    applied,
                )
            )
        elif contradict_no_change:
            # The cause fired from old_v under a full settle window and the
            # register did not move — falsify any learned edge claiming it
            # would (a static-catalog route ignores unreadable enablers),
            # and mark the probe so it is not re-sent.
            observations.append(
                CompassObservation(
                    "contradict",
                    n.tag,
                    cause,
                    old_v,
                    None,
                    world_key,
                    tuple(sorted(before_snap.items())),
                    applied,
                )
            )
    return tuple(observations)


# ---------------------------------------------------------------------------
# Try-verify wrappers
# ---------------------------------------------------------------------------


def _try_action_batch(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    *,
    record_influence_action: Action | None = None,
) -> _AttemptResult:
    policy = bearing.act.policy
    # ── Action gate (avoid=) ──────────────────────────────────────────────
    # Before the pulse: a candidate whose overlaid action makes the avoid
    # predicate true is a path that depends on the avoided condition — reject it
    # *without* pressing, so a momentary command (avoid=C_Complete) is never
    # pulsed.  Static: overlay the applied set onto the live snapshot and read
    # the predicate.  nogood the choice so the next iteration stops surfacing it
    # (candidates filters nogoods), and record the names so the terminal decline
    # can point at what excluded the path.
    avoid_names = _avoid_violations(ctx, policy.applied, frame.snap)
    if avoid_names:
        return _AttemptResult(
            trial=None,
            gate_events=(
                PilotGateEvent("avoid", f"action would enter avoid: {', '.join(avoid_names)}"),
            ),
            nogood_pairs=(
                frozenset({policy.nogood_pair}) if policy.nogood_pair is not None else frozenset()
            ),
            avoid_names=tuple(avoid_names),
        )

    trial = _apply_actions(policy.applied, frame, state, ctx)
    key_config = state.key_config
    assert key_config is not None

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
                world_key=_pilot_world_key(frame.snap, key_config, state.pilot_rungs),
                applied=policy.applied,
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
                world_key=_pilot_world_key(wait_before, key_config, state.pilot_rungs),
            )
        )
        wait_before = wait_after

    result = verify_gates(
        _ExecutedAttempt(pulse=trial, bearing=bearing),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=tuple(observations))


def execute(bearing: Bearing, world: OrientationWorld) -> _AttemptResult:
    """Execute exactly the act declared by a current-world bearing.

    This is deliberately narrower than orientation: it validates the world
    binding, installs declared prerequisites, and dispatches one act through
    the existing verification pipeline.  It never selects a fallback and
    applies the declared ``ActPolicy`` without decoding its provenance.
    """

    frame = world.frame
    state = world.state
    ctx = world.context
    key_config = state.key_config
    if key_config is None:
        raise StaleBearingError("cannot execute a bearing before the world key is configured")
    live_key = _pilot_world_key(
        dict(state.work.state.tags),
        key_config,
        state.pilot_rungs,
    )
    if live_key != bearing.world_key:
        raise StaleBearingError(
            f"bearing world {bearing.world_key!r} is stale; current world is {live_key!r}"
        )

    if bearing.prerequisites:
        _install_prerequisites(state, tuple(bearing.prerequisites))

    act = bearing.act
    if isinstance(act, Pulse):
        return _try_action_batch(
            bearing,
            frame,
            state,
            ctx,
            record_influence_action=act.action,
        )
    if isinstance(act, BatchPulse):
        return _try_action_batch(
            bearing,
            frame,
            state,
            ctx,
        )
    if isinstance(act, Coast):
        if act.mode == "bearing":
            return _try_bearing_coast(
                bearing,
                frame,
                state,
                ctx,
            )
        return _try_terminal_letrun(bearing, frame, state, ctx)
    if isinstance(act, Dwell):
        return _try_terminal_dwell(bearing, frame, state, ctx)
    raise TypeError(f"unsupported navigation act {type(act).__name__}")


# ---------------------------------------------------------------------------
# Bearing coast — cross timer/step-counter plateaus
# ---------------------------------------------------------------------------


def _try_bearing_coast(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Run a bearing coast through the verify pipeline.

    Forks, coasts past timer/step-counter plateaus, then runs the shared
    verify gates. The outcome classifier sees coast results the same way it
    sees command results: SPIN if nothing moved, CONFIRMED if the channel
    register transitioned forward, AMBIENT_DRIFT if the program ejected.

    An ejection (e.g. S_StateCurrent 3→9) is AMBIENT_DRIFT with trend
    regression.  ``_monitor_trend`` reverts to the last checkpoint; a future
    investigation layer should own bounded incident analysis and replay-tested
    corrective holds.
    """
    coast = bearing.act
    assert isinstance(coast, Coast)
    heading = coast.policy.heading
    route = heading.route if heading is not None else None
    channel_tag = heading.channel_tag if heading is not None else None
    target_value = heading.target_value if heading is not None else None
    boundary = heading.boundary if heading is not None else None
    route_channel_tag = route.channel_tag if route is not None else None
    fork = fork_with_rungs(state.work, state.pilot_rungs)
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)

    # Confirmed conditional holds (oscillation correctives) animate during the
    # channel coast, same as the terminal let-run — fork_with_rungs installs
    # only the steady half.
    session = CoastSession(fork, kind="bearing_coast")
    session.arm_avoid(ctx.avoid_pred)
    session.arm_pens(_pen_tags(state, ctx))
    dwell, bearing_coast_receipt = _coast_to_bearing(
        fork,
        channel_tag,
        target_value,
        watched_tags=_watched_tags(frame, ctx),
        session=session,
        boundary=boundary,
        route_channel_tag=route_channel_tag,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(snap_after, key_config, state.pilot_rungs)

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
                world_key=_pilot_world_key(wait_before, key_config, state.pilot_rungs),
            )
        )
        wait_before = wait_after

    departed_route = (
        bearing_coast_receipt is not None
        and bearing_coast_receipt.stop_reason == "departed"
        and route is not None
    )
    verify_channel = channel_tag
    verify_target = target_value
    if departed_route and route is not None:
        verify_channel = route.channel_tag
        verify_target = route.target_value

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
        coast_receipt=bearing_coast_receipt,
        timeline=session.events,
        channel_motion=ChannelMotion(
            verify_channel,
            verify_target,
            boundary,
        ),
    )

    result = verify_gates(
        _ExecutedAttempt(pulse=trial, bearing=bearing),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=tuple(observations))


def _try_terminal_letrun(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Generalized terminal let-run — the bottom-of-loop fallback.

    Reached here when no route bearing coast, command candidate, or widening made
    progress, yet the watched tags are still live (things pending). The only move left is
    to hold the current macro-state and coast toward the global target, letting
    the program's self-advancing sub-processes (timers, step-counters) complete.

    Nothing about intermediate bearings is assumed: the heading is the global
    target, and the ejection guard is the recognized state-machine roles held at
    their current values.  Outcomes route through the shared verify pipeline:

      - target reached  -> CONFIRMED (the global-target check in verify_gates).
      - macro-state left -> AMBIENT_DRIFT; commit + _monitor_trend hands the
        ejection to investigation (the same path the doors took).
      - stall (budget, no target, no ejection) -> dead-end reject; the caller
        falls back to a bounded watched-tag settle.
    """
    role_tags = coast_departure_tags(state, ctx)
    # fork_with_rungs re-establishes the steady holds on the coast fork: force
    # overrides do not propagate through fork(), and a freshly-installed
    # prerequisite — e.g. the Enable that drives a harness sensor's ramp — has not
    # been scanned onto state.work yet, so its value isn't carried either.
    fork = fork_with_rungs(state.work, state.pilot_rungs)
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
        (lambda s: target_reached(s.tags, ctx.target.tag, ctx.target.value, ctx.target.predicate))
        if ctx.target.predicate is not None
        else None
    )

    budget = min(
        _COAST_BUDGET,
        max(
            2,
            state.remaining_search_scans(ctx.max_scans, scan_id=scan_before),
        ),
    )
    session = CoastSession(fork, kind="letrun")
    session.arm_avoid(ctx.avoid_pred)
    session.arm_pens(_pen_tags(state, ctx))
    letrun_receipt = _coast_holding_state(
        fork,
        ctx.target.tag,
        ctx.target.value,
        role_tags,
        budget=budget,
        reached_fn=reached_fn,
        session=session,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(snap_after, key_config, state.pilot_rungs)

    observations = _compass_observations(
        WAIT,
        frame,
        snap_before,
        snap_after,
        ctx,
        contradict_no_change=False,
        world_key=_pilot_world_key(snap_before, key_config, state.pilot_rungs),
    )

    # Decide the outcome here — only the let-run knows the macro-state sentinel.
    #   reached  -> let the global-target check in verify_gates accept (CONFIRMED).
    #   ejected  -> a role left its held value: AMBIENT_DRIFT, handed to
    #               investigation via the changed role as the deviation bearing.
    #   stall    -> nothing reached, no role moved: a true dead end; let the
    #               caller fall back to a bounded watched-tag settle.
    reached = target_reached(snap_after, ctx.target.tag, ctx.target.value, ctx.target.predicate)
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
        channel_motion=ChannelMotion(chan_tag, chan_val),
    )

    result = verify_gates(
        _ExecutedAttempt(pulse=trial, bearing=bearing),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=observations)


def _try_terminal_dwell(
    bearing: Bearing,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
) -> _AttemptResult:
    """Run one bounded repeated dwell through the shared trial gates.

    Reached only when Compass knowledge carries a terminal-coast receipt for
    this world key. The coast is deterministic under the held inputs,
    so repeating the full ejection-guarded let-run would reproduce the same
    departure.

    Perform one deterministic watched-tag settle on a fork and route it through the
    same :func:`verify_gates` target gate as terminal let-run:

      - a self-advancing frontier that crosses the target during the dwell is
        CONFIRMED through the shared target gate (verify stays the sole source);
      - anything else is a legible terminal stall (dead-end reject), handed back
        to the caller's skiff / stuck exit.

    No ejection is committed and no investigation re-runs, so the loop cannot spin
    re-ejecting: a non-completing dwell terminates at the stuck exit rather than
    repeatedly spending the invocation's remaining search budget.
    """
    fork = fork_with_rungs(state.work, state.pilot_rungs)
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)

    def _reached(tags: dict[str, Any]) -> bool:
        return target_reached(tags, ctx.target.tag, ctx.target.value, ctx.target.predicate)

    ceiling = min(
        _LETRUN_DWELL_CEILING,
        max(
            2,
            state.remaining_search_scans(ctx.max_scans, scan_id=scan_before),
        ),
    )
    session = CoastSession(fork, kind="settle")
    session.arm_pens(_pen_tags(state, ctx))
    dwell = _settle_watched_tags(
        fork,
        _watched_tags(frame, ctx),
        floor=2,
        ceiling=ceiling,
        reached_fn=_reached,
        session=session,
    )

    snap_after = dict(fork.state.tags)
    key_config = state.key_config
    assert key_config is not None
    key_after = _pilot_world_key(snap_after, key_config, state.pilot_rungs)

    observations = _compass_observations(
        WAIT,
        frame,
        snap_before,
        snap_after,
        ctx,
        contradict_no_change=False,
        world_key=_pilot_world_key(snap_before, key_config, state.pilot_rungs),
    )

    if not _reached(snap_after):
        # No new input is possible here and the watched tags quiesced without crossing the
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
        wait_snaps=tuple(dwell),
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
        _ExecutedAttempt(pulse=trial, bearing=bearing),
        frame,
        state,
        ctx,
    )
    return replace(result, observations=observations)


def _coast_to_bearing(
    work: PLC,
    channel_tag: str | None,
    target_value: Any,
    watched_tags: frozenset[str],
    session: CoastSession | None = None,
    *,
    boundary: Any = None,
    route_channel_tag: str | None = None,
) -> tuple[list[dict[str, Any]], Any]:
    """Coast the live state past timer/step-counter plateaus.

    The bearing coast has its own generous budget (``_COAST_BUDGET``) — it does NOT
    consume the pilot's iteration budget.  Timer dwell is waiting, not
    searching.

    With a channel register and target value, seek with the target and
    departure triggers armed — the coast lands on the exact scan either fires
    and the returned receipt says which.  Without a channel register, fall
    back to the bounded single-step watched-tag settle (no receipt — outcome's
    settle-path arm depends on its absence; the session still records pens).

    Returns ``(trajectory, receipt_or_None)``.
    """
    if channel_tag is None:
        return (
            _settle_watched_tags(
                work,
                watched_tags,
                floor=2,
                ceiling=_LETRUN_DWELL_CEILING,
                session=session,
            ),
            None,
        )

    budget = _COAST_BUDGET
    if boundary is not None:
        from pyrung.core.instruction.advance import constraint_holds

        estimate = estimate_owned_boundary_scans(work, boundary)
        if estimate is not None:
            budget = max(budget, estimate + 2)
        receipt = _coast_holding_state(
            work,
            channel_tag,
            target_value,
            ((route_channel_tag,) if route_channel_tag is not None else ()),
            budget=budget,
            reached_fn=lambda state: constraint_holds(boundary, state.tags) is True,
            reached_condition=_constraint_condition(work, boundary),
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
