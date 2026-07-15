"""ACT — the steering phase of the PILOT loop.

Cone settlement, pulse execution, zoom through timer plateaus, try-verify
wrappers, and candidate value proposals.  Everything the pilot does to test
a bearing or coast through a dwell.  ACT is where execution lives; ORIENT
(trace + compass) only reads.

Act never writes the compass: the wrappers gather ``CompassObservation``
values onto the returned ``_AttemptResult``; the loop's RECORD point applies
them.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot._ops import (
    _ZOOM_BUDGET,
    _avoid_violations,
    _coast_holding_state,
    _coast_to_value,
    _DebugFn,
    _pilot_world_key,
    _settle_delayed_effects,
    fork_with_rungs,
    wait_edge_nogood,
)
from pyrung.core.analysis.pilot.trace import _all_nodes, target_reached

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.candidates import _Candidate, _CandidateList
from pyrung.core.analysis.pilot.causal import chase_cause_roots
from pyrung.core.analysis.pilot.compass import WAIT, Action, CompassObservation, is_action
from pyrung.core.analysis.pilot.types import (
    MotionKind,
    PilotGateEvent,
    _ActionPair,
    _AttemptResult,
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

_SETTLE_CONE_CEILING = 16
_LETRUN_DWELL_CEILING = 64


def _settle_cone(
    fork: PLC,
    cone: frozenset[str],
    *,
    floor: int = 2,
    ceiling: int = _SETTLE_CONE_CEILING,
    reached_fn: Callable[[dict[str, Any]], bool] | None = None,
) -> list[dict[str, Any]]:
    """Coast *fork* until the cone stops moving — dwell control only.

    Logic can take up to two scans to propagate, so step ``floor`` scans before
    judging anything.  After the floor, step one scan at a time and stop as soon
    as no tag in *cone* changed since the previous scan (a cone fixpoint), or
    once ``ceiling`` scans have run.  Returns the per-scan trajectory.

    ``reached_fn`` short-circuits the dwell: a one-scan transient target (the
    machine passes through ``STARTING`` for a single scan on its way to
    ``EXECUTE``) is otherwise blown past by the cone-fixpoint coast, and the
    post-settle ``target_reached`` check never sees it.  Stopping the scan the
    target holds lands the fork *on* the transient so the trial is CONFIRMED.

    Settle never accepts or rejects.  Attributing the trajectory to one of the
    five verify outcomes — who moved what — is the caller's job via ``cause()``.
    """
    ceiling = max(floor, ceiling)
    snaps: list[dict[str, Any]] = []
    prev = dict(fork.state.tags)
    for i in range(ceiling):
        fork.step()
        cur = dict(fork.state.tags)
        snaps.append(cur)
        if reached_fn is not None and reached_fn(cur):
            break
        if i + 1 >= floor and all(cur.get(t) == prev.get(t) for t in cone):
            break
        prev = cur
    return snaps


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
    patch = {t: v for t, v in actions}
    needs_edge = any(t in ctx.edge_tags for t in patch)

    if needs_edge:
        release = {t: ctx.resting.get(t, False) for t in patch if t in ctx.edge_tags}
        if release:
            fork.patch(release)
            fork.step()

    fork.patch(patch)
    fork.step()
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
        wait_snaps = _settle_cone(fork, _cone_tags(frame, ctx), floor=2, reached_fn=_reached)

    post_pulse_snap = dict(fork.state.tags)
    post_pulse_key = _pilot_world_key(post_pulse_snap, key_config, state.rungs)
    if not _reached(post_pulse_snap):
        _settle_delayed_effects(
            fork,
            frame.snap,
            key_config,
            scan_budget=ctx.max_scans - fork.state.scan_id,
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
    )


# ---------------------------------------------------------------------------
# Compass observation gathering — Act observes; RECORD applies
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
    """Observe compass-relevant motion between two snapshots — never applies.

    The causal chase (control vs wind) is evidence-*gathering*, so it happens
    here; the write happens at the loop's RECORD point.
    """
    action_tag = cause[0] if is_action(cause) else None
    observations: list[CompassObservation] = []
    for n in _all_nodes(frame.tree):
        # pipeline_internal nodes are included: the learned table is the
        # pipeline instrument's own memory, and a live trial is the strongest
        # evidence there is — both for new edges and for falsifying stale
        # statically-seeded ones.
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
            # would (a statically-seeded route ignores unreadable enablers),
            # and mark the probe so it is not re-sent.
            observations.append(CompassObservation("contradict", n.tag, cause, old_v))
    return tuple(observations)


# ---------------------------------------------------------------------------
# Try-verify wrappers
# ---------------------------------------------------------------------------


def _label_action(action_pairs: tuple[_ActionPair, ...]) -> str:
    if len(action_pairs) == 1:
        t, v = action_pairs[0]
        return f"({t}={v!r})"
    return f"({', '.join(f'{t}={v!r}' for t, v in action_pairs)})"


def _try_action_batch(
    action_pairs: tuple[_ActionPair, ...],
    applied: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
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
        dbg(f"#     AVOID-ACTION {debug_name}: would enter {', '.join(avoid_names)}")
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
        dbg,
        observe_label=observe_label,
        target_observe_label=target_observe_label,
        debug_name=debug_name,
        influence_prescribed=influence_prescribed,
        route_prescribed=route_prescribed,
        nogood_pair=nogood_pair,
        regression_nogoods=regression_nogoods,
        chase_regression_causes=chase_regression_causes,
        zoom_channel_tag=bearing_channel_tag,
        zoom_target_value=bearing_channel_value,
    )
    return replace(result, observations=tuple(observations))


def _try_candidate(
    candidate: _Candidate,
    candidates: _CandidateList,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    from pyrung.core.analysis.pilot.candidates import _candidate_applied

    pair = candidate.pair
    applied = _candidate_applied(candidate, candidates, ctx)
    if len(applied) > 1:
        dbg(f"#     INFLUENCE-CONTEXT: +{len(candidates.trace_actions)} trace actions")

    return _try_action_batch(
        (pair,),
        applied,
        frame,
        state,
        ctx,
        dbg,
        observe_label="accept",
        target_observe_label="target",
        debug_name=_label_action((pair,)),
        influence_prescribed=candidate.influence_prescribed,
        route_prescribed=candidate.route_prescribed,
        nogood_pair=pair,
        regression_nogoods=frozenset({pair}),
        chase_regression_causes=True,
        record_influence_action=pair,
        bearing_channel_tag=candidate.bearing_channel_tag,
        bearing_channel_value=candidate.bearing_channel_value,
    )


def _try_prescribed_batch(
    batch: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    """Try a skiff-prescribed composite edge as one live batch trial.

    The isolated experiment identified this action *set* as the joint cause of
    a frontier edge; the members must fire in the same window, so they ride
    one batch through the same gate pipeline as any candidate — the learned
    edge stays a bearing until the live verify confirms it.
    """
    dbg(f"# --- Skiff batch ({len(batch)} actions) ---")
    return _try_action_batch(
        batch,
        batch,
        frame,
        state,
        ctx,
        dbg,
        observe_label="batch",
        target_observe_label="batch-target",
        debug_name="SKIFF-BATCH",
        influence_prescribed=True,
        route_prescribed=False,
        nogood_pair=None,
        regression_nogoods=frozenset(batch),
        chase_regression_causes=False,
    )


def _try_widening(
    active_trace_actions: tuple[_ActionPair, ...],
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
) -> _AttemptResult:
    all_nogoods: list[_ActionPair] = []
    all_observations: list[CompassObservation] = []
    for width in range(2, len(active_trace_actions) + 1):
        batch = active_trace_actions[:width]
        dbg(f"# --- Width {width} ({len(batch)} actions) ---")
        attempt = _try_action_batch(
            batch,
            batch,
            frame,
            state,
            ctx,
            dbg,
            observe_label="width",
            target_observe_label="width-target",
            debug_name=f"WIDTH-{width}",
            influence_prescribed=False,
            route_prescribed=False,
            nogood_pair=None,
            regression_nogoods=frozenset(batch),
            chase_regression_causes=False,
        )
        all_nogoods.extend(attempt.nogood_pairs)
        all_observations.extend(attempt.observations)
        if attempt.trial is not None:
            # Earlier (rejected) widths executed too — their observations ride
            # along so RECORD commits them with the accepted width's.
            return replace(attempt, observations=tuple(all_observations))
    return _AttemptResult(
        trial=None,
        nogood_pairs=frozenset(all_nogoods),
        observations=tuple(all_observations),
    )


# ---------------------------------------------------------------------------
# Zoom — coast past timer/step-counter plateaus
# ---------------------------------------------------------------------------


def _try_zoom(
    candidates: _CandidateList,
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
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
    channel_tag = (
        candidates.route_plan.role.channel_tag if candidates.route_plan is not None else None
    )
    target_value = (
        candidates.route_plan.first_edge.to_value if candidates.route_plan is not None else None
    )

    fork = fork_with_rungs(state.work, state.rungs)
    scan_before = fork.state.scan_id
    snap_before = dict(fork.state.tags)

    # A rejected wait is evidence about THIS world: the edge did not complete
    # here (a recipe-gated automatic transition, a dwell that never arms).
    # Record it as a world-keyed nogood so the next ORIENT's route query walks
    # around the edge instead of re-burning the same sterile coast.
    wait_nogood = (
        wait_edge_nogood(channel_tag, snap_before.get(channel_tag), target_value)
        if channel_tag is not None
        else None
    )

    # Confirmed conditional holds (oscillation correctives) animate during the
    # channel coast, same as the terminal let-run — fork_with_rungs installs
    # only the steady half.
    dwell = _letrun_zoom(fork, channel_tag, target_value, cone=_cone_tags(frame, ctx))

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
    )

    result = verify_gates(
        trial,
        action_pairs=(),
        applied=(),
        frame=frame,
        state=state,
        ctx=ctx,
        dbg=dbg,
        observe_label="zoom",
        target_observe_label="zoom-target",
        debug_name="ZOOM",
        influence_prescribed=False,
        route_prescribed=candidates.route_plan is not None,
        nogood_pair=wait_nogood,
        regression_nogoods=frozenset(),
        chase_regression_causes=True,
        zoom_channel_tag=channel_tag,
        zoom_target_value=target_value,
        motion=MotionKind.COAST_TO_BEARING,
    )
    return replace(result, observations=tuple(observations))


def _try_terminal_letrun(
    frame: _IterationFrame,
    state: _PilotState,
    ctx: _PilotContext,
    dbg: _DebugFn,
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
    role_tags = tuple(r.channel_tag for r in ctx.pipeline_roles)
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
    _coast_holding_state(
        fork,
        ctx.target_tag,
        ctx.target_value,
        role_tags,
        budget=budget,
        reached_fn=reached_fn,
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
    changed_role = next(
        (t for t in role_tags if not _values_match(snap_after.get(t), start_roles[t])),
        None,
    )
    if not reached and changed_role is None:
        return _AttemptResult(
            trial=None,
            gate_events=(PilotGateEvent("dead-end", "terminal stall, no ejection"),),
            observations=observations,
        )

    if reached:
        chan_tag: str | None = None
        chan_val: Any = None
    else:
        assert changed_role is not None
        chan_tag = changed_role
        chan_val = start_roles[changed_role]

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
    )

    result = verify_gates(
        trial,
        action_pairs=(),
        applied=(),
        frame=frame,
        state=state,
        ctx=ctx,
        dbg=dbg,
        observe_label="letrun",
        target_observe_label="letrun-target",
        debug_name="TERMINAL-LETRUN",
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
    dbg: _DebugFn,
) -> _AttemptResult:
    """Re-coast dwell — the verified form of the old bare cone settle.

    Reached only when terminal let-run already ran at this key with these holds
    (``letrun_tried[key] >= len(rungs)``).  The coast is deterministic
    given the held inputs, so re-running the ejection-guarded let-run would only
    re-eject and re-investigate; the old code side-stepped that with a bare
    ``_settle_cone`` straight onto ``state.work`` — the one execution that skipped
    verify (and let the loop's top-of-scan reached check confirm a coast the
    verify target gate never saw).

    Instead, do ONE bounded, deterministic cone settle on a *fork* and route it
    through the same :func:`verify_gates` target gate as terminal let-run:

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
    _settle_cone(fork, _cone_tags(frame, ctx), floor=2, ceiling=ceiling, reached_fn=_reached)

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
        dbg("#     TERMINAL-DWELL: settled without reaching target")
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
        dbg=dbg,
        observe_label="letrun",
        target_observe_label="letrun-target",
        debug_name="TERMINAL-DWELL",
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
) -> list[dict[str, Any]]:
    """Coast the live state past timer/step-counter plateaus.

    The zoom has its own generous budget (``_ZOOM_BUDGET``) — it does NOT
    consume the pilot's iteration budget.  Timer dwell is waiting, not
    searching.

    With a channel register and target value, install a ``when().pause()``
    guard for ejection (channel tag goes somewhere unexpected), then
    ``run_until`` the target.  If the guard fires first, the zoom stops
    immediately at the ejection scan — no budget wasted.

    Without a channel register, fall back to the bounded single-step cone
    settle.
    """
    if channel_tag is None:
        return _settle_cone(work, cone, floor=2, ceiling=_LETRUN_DWELL_CEILING)

    _coast_to_value(work, channel_tag, target_value, budget=_ZOOM_BUDGET)
    return [dict(work.state.tags)]
