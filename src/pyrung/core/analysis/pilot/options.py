"""Materialize and rank the action and wait options for one orientation.

``_build_candidates`` combines the current trace tree, constrained static
routes, learned transitions, program-awaited actions, existing corrections,
and prerequisite holds. It returns their priority order together with any
prescribed wait, completion frontier, or no-bearing diagnosis.

Candidate construction reads the current world and knowledge but does not
execute a trial, apply observations, or commit state.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pilot._ops import (
    PilotRung,
    _atom_condition,
    _avoid_forces,
    _rung_execution_receipt,
    _target_unresolved_condition,
    _until_unresolved_condition,
    wait_edge_nogood,
)
from pyrung.core.analysis.pilot.compass import (
    is_action,
    is_composite_action,
)
from pyrung.core.analysis.pilot.trace import (
    _all_nodes,
    _WriterAvailability,
    frontier_pairs,
    trace_back,
)
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.charts import StaticPath
    from pyrung.core.analysis.pilot.trace import TraceAction

# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    tag: str
    value: Any
    influence_prescribed: bool = False
    provenance: tuple[str, ...] = ()
    wake: int | None = None
    route_prescribed: bool = False
    # The first compass edge's executable promise. Trial verification uses this for every
    # route-prescribed action, not only a coast/zoom: landing elsewhere means
    # the program displaced the route and must be investigated.
    bearing_channel_tag: str | None = None
    bearing_channel_value: Any = None
    # A program-owned current (currents.py): the one operator action the program
    # is dwelling on at the current state of an opaque-loop channel, surfaced when
    # the trace dead-ends and the compass route is the avoided command.  Ordered
    # like a prescribed edge (a recognized bearing), but below route/influence so
    # it is the fallback, never overriding an available route.
    current_prescribed: bool = False
    current_note: str = ""
    # An external input required by the exact program producer selected for an
    # automatic route edge. It is a current-world bearing below an established
    # route/current and above an unrelated trace action.
    program_prescribed: bool = False
    program_note: str = ""
    program_context_actions: tuple[_ActionPair, ...] = ()
    # Rank rationale — recorded at the scoring site (``_build_candidates``) and
    # surfaced through ``recording._candidate_payload`` so every candidate event carries why
    # it sorted where it did.  ``scored`` is False for a prescribed edge (the
    # compass' explicit bearing), which *bypasses* scoring: ``avail_tier`` /
    # ``over_wake`` / ``compass_score`` are then the forced (0, False, (0, 0))
    # bypass values, not measured ones.  ``None`` before scoring runs.
    avail_tier: int | None = None
    over_wake: bool | None = None
    compass_score: tuple[int, int] | None = None
    scored: bool | None = None

    @property
    def pair(self) -> _ActionPair:
        return (self.tag, self.value)


@dataclass(frozen=True)
class _CandidateList:
    active_trace_actions: tuple[_ActionPair, ...]
    trace_actions: tuple[_ActionPair, ...]
    trace_action_details: tuple[TraceAction, ...]
    route_candidates: tuple[_ActionPair, ...]
    candidates: tuple[_Candidate, ...]
    wake_cap: int
    route_plan: StaticPath | None = None
    wait_prescribed: bool = False
    wait_reason: str | None = None
    # An instruction-owned frontier's immediate observable boundary. This is
    # one coast heading, not a route: after it lands PILOT retraces.
    advance_boundary: _ActionPair | None = None
    advance_condition: Any = None
    # A composite learned edge (skiff pair probe): the whole action set must
    # fire in one window.  Tried as a single batch trial before the singles —
    # verified live through the same gate pipeline as any candidate.
    prescribed_batch: tuple[_ActionPair, ...] | None = None
    prerequisite_rungs: tuple[PilotRung, ...] = ()
    stuck_reason: str | None = None
    # Co-actions that must fire in the same scan as a route-prescribed command
    # candidate (the one-shot edge gate, e.g. ``rise(CmdChgRequest)``).  Carried
    # off the chosen compass edge so the command rung actually executes.
    route_co_actions: tuple[_ActionPair, ...] = ()
    # Convergence command buttons currently held off-resting.  A command decoder
    # is last-write-wins, so pressing one button while another is still held
    # fires the wrong command; a convergence pulse releases these.
    held_command_tags: frozenset[str] = frozenset()
    # Stage-1 command actions deferred this iteration because a stage-0 establish
    # gate is still pending (diagnostic only — the drive loop never sees them).
    deferred_commands: tuple[_ActionPair, ...] = ()
    # The completion re-read's unmet frontier: a prescribed wait's charted
    # completion condition, re-traced against the live world (``_prescribe_wait``).
    # Names the pressable lever behind the wait (``x_RotateFB``) so the terminal
    # frontier clause points past the pipeline cut. Orientation stamps it onto
    # its completed frame; empty when no wait carries completion.
    completion_frontier: tuple[_ActionPair, ...] = ()
    # Exact-producer reading behind this iteration's automatic edge. The
    # orientation layer consumes its witnessed local boundary; recording keeps
    # the same value available for diagnostics.
    program_step: Any = None


@dataclass(frozen=True)
class _WaitPrescription:
    """One grounded wait decision and the evidence that justified it."""

    prescribed: bool
    reason: str | None = None
    details: tuple[TraceAction, ...] = ()
    frontier: tuple[_ActionPair, ...] = ()
    program_step: Any = None
    boundary: Any = None


# ---------------------------------------------------------------------------
# Stuck taxonomy
# ---------------------------------------------------------------------------

_STUCK_TRACE_OPAQUE = "trace_opaque"
_STUCK_TRACE_EMPTY = "trace_empty"
_STUCK_TRACE_GUARD = "trace_guard"
_STUCK_COMPASS_NO_ROUTE = "compass_no_route"
_STUCK_ZOOM_REJECTED = "zoom_rejected"


def _diagnose_stuck_reason(
    frame: Any,
    ctx: Any,
) -> str | None:
    """Classify *why* the instruments can't produce a bearing.

    Returns ``None`` when the tree has steerable leaves (not stuck).
    """
    tree = frame.tree
    leaves = list(tree.leaves())
    steerable = [n for n in leaves if n.is_steerable and not n.satisfied]
    if steerable:
        return None

    # A self-advancing (coast) leaf means let-run, not trace, owns the bearing —
    # a converging frontier (timer/counter Acc, or a harness-linked ramp toward a
    # threshold).  Not stuck: escalate to the terminal let-run rather than bail at
    # a dead-end.  This is the trace -> let-run rung of the compass escalation.
    if any(getattr(n, "advance", None) is not None and not n.satisfied for n in leaves):
        return None

    satisfied = [n for n in leaves if n.satisfied]
    if len(satisfied) == len(leaves) and leaves:
        return None

    # Writer found, all conditions satisfied (empty children) — the output
    # instruction just hasn't fired yet. This readiness has the same meaning at
    # every trace depth: a timer result can make a nested Step writer ready one
    # scan before the outer target writer observes Step. Let the loop coast.
    if any(
        node.writer_rung is not None
        and not node.children
        and not node.satisfied
        and not node.is_steerable
        for node in _all_nodes(tree)
    ):
        return None

    dead_ends = [
        n
        for n in leaves
        if not n.satisfied and not n.is_steerable and not getattr(n, "pipeline_internal", False)
    ]
    if not dead_ends:
        has_writers = any(ctx.pdg.writers_of.get(n.tag) for n in leaves if not n.satisfied)
        if not has_writers:
            return _STUCK_TRACE_EMPTY
        return _STUCK_TRACE_GUARD

    for n in dead_ends:
        if ctx.pdg.writers_of.get(n.tag):
            return _STUCK_TRACE_OPAQUE

    return _STUCK_TRACE_EMPTY


# ---------------------------------------------------------------------------
# Compass scoring / routing
# ---------------------------------------------------------------------------


def _learned_edge_allowed(
    tag: str,
    source: Any,
    cause: Any,
    destination: Any,
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> bool:
    """Apply every live constraint before a learned edge enters any path query."""

    if is_action(cause):
        members = cast(tuple[_ActionPair, ...], cause) if is_composite_action(cause) else (cause,)
        return all(
            pair not in key_nogoods
            and ctx.route_allowed(pair)
            and not _avoid_forces(ctx, (pair,), frame.snap)
            for pair in members
        )
    return wait_edge_nogood(tag, source, destination) not in key_nogoods


def _compass_score(
    pair: _ActionPair,
    frame: Any,
    ctx: Any,
) -> tuple[int, int]:
    """Rank candidates by learned transition progress for current needs.

    Ordering, never rejection — the worst this returns is a high tier, so a
    backward move is tried last, not vetoed.  A known move of a *channel*
    register (``opaque_loop``) dominates: if the action drives a channel
    register away from the goal, it ranks backward even when it incidentally
    advances some lesser sub-need (steering ``C_Clear`` toward Stopped must not
    look like progress just because it ticks an unrelated flag).
    """
    chan_forward: tuple[int, int] | None = None
    chan_back: tuple[int, int] | None = None
    best_forward: tuple[int, int] | None = None
    best_regression: tuple[int, int] | None = None
    saw_known = False
    saw_no_change = False
    key_nogoods = set(ctx.compass.knowledge.nogood_pairs(frame.key))
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        cur_val = frame.snap.get(n.tag)
        if _values_match(cur_val, n.value):
            continue

        dest = ctx.compass.transition_dest(
            n.tag,
            cur_val,
            pair,
            world_key=frame.key,
            snapshot=frame.snap,
        )
        if dest is None:
            if pair in ctx.compass.probed_actions(
                n.tag,
                cur_val,
                world_key=frame.key,
                snapshot=frame.snap,
            ):
                saw_no_change = True
            continue

        saw_known = True
        if _values_match(dest, n.value):
            score = (0, 0)
        else:
            edge_allowed = lambda source, cause, destination, tag=n.tag: _learned_edge_allowed(
                tag,
                source,
                cause,
                destination,
                frame,
                ctx,
                key_nogoods,
            )
            forward = ctx.compass.find_path(
                n.tag,
                dest,
                n.value,
                cause_allowed=edge_allowed,
                world_key=frame.key,
                snapshot=frame.snap,
            )
            if forward:
                score = (1, len(forward))
            else:
                back = ctx.compass.find_path(
                    n.tag,
                    dest,
                    cur_val,
                    cause_allowed=edge_allowed,
                    world_key=frame.key,
                    snapshot=frame.snap,
                )
                if not back:
                    continue
                score = (150, len(back))

        backward = score[0] >= 150
        if n.tag in ctx.opaque_loop:
            if backward:
                chan_back = score if chan_back is None else min(chan_back, score)
            else:
                chan_forward = score if chan_forward is None else min(chan_forward, score)
        elif backward:
            best_regression = score if best_regression is None else min(best_regression, score)
        else:
            best_forward = score if best_forward is None else min(best_forward, score)

    if chan_forward is not None:
        return chan_forward
    if chan_back is not None:
        return chan_back
    if best_forward is not None:
        return best_forward
    if best_regression is not None:
        return best_regression
    if saw_known:
        return (25, 0)
    if saw_no_change:
        return (200, 0)
    return (50, 0)


def _availability_tier(detail: TraceAction | None) -> int:
    """Demotion tier from a leaf's worst-on-path writer availability.

    ``AVAILABLE_NOW`` / ``AFTER_PREREQ`` chains (tier 0) try before ``UNKNOWN``
    chains (tier 1) before ``UNAVAILABLE_FROM_HERE`` chains (tier 2).  A leaf with
    no trace detail (a route/influence candidate carried off the compass, not the
    tree) is treated as tier 0 — its ordering is owned by the prescribed keys, not
    by this signal.  Ordering only: nothing is ever dropped.
    """
    if detail is None:
        return 0
    avail = detail.availability
    if avail <= _WriterAvailability.AFTER_PREREQ:
        return 0
    if avail == _WriterAvailability.UNKNOWN:
        return 1
    return 2


def _compass_route_plan(
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair] | None = None,
    excluded_edges: frozenset[tuple[Any, ...]] = frozenset(),
) -> StaticPath | None:
    if not ctx.compass.graphs:
        return None

    from pyrung.core.analysis.pilot.charts import _best_static_path

    nogoods = key_nogoods if key_nogoods is not None else set()

    def _edge_open(edge: Any) -> bool:
        if edge.identity in excluded_edges:
            return False
        if ctx.compass.knowledge.static_edge_status(
            edge, getattr(frame, "key", None), frame.snap
        ) in {
            "contradicted",
            "no_change",
        }:
            return False
        if edge.action is None:
            # A completion edge proven sterile at this world (a rejected wait)
            # is walked around, exactly like a nogood press — BFS then returns
            # the surviving route.
            return (
                wait_edge_nogood(edge.role.channel_tag, edge.from_value, edge.to_value)
                not in nogoods
            )
        return ctx.route_allowed(edge.action) and not _avoid_forces(ctx, [edge.action], frame.snap)

    plans: list[StaticPath] = []
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        if not n.children and n.tag not in ctx.opaque_loop:
            continue
        if _values_match(frame.snap.get(n.tag), n.value):
            continue
        plan = _best_static_path(
            n.tag,
            n.value,
            frame.snap,
            ctx.compass.graphs,
            edge_allowed=_edge_open,
        )
        if plan is not None:
            plans.append(plan)

    if not plans:
        return None
    return min(
        plans,
        key=lambda p: (_plan_off_target(p, ctx), _plan_ungrounded(p), _route_plan_score(p)),
    )


def _edge_grounded(edge: Any) -> bool:
    """Whether *edge* carries a concrete from-value (not the ``ANY_FROM`` sentinel).

    Only a grounded edge is a coastable (WAIT-prescribable) claim: a wildcard edge
    says nothing about the state the register advances *from*, so "hold and wait"
    on it has no dwell semantics.
    """
    from pyrung.core.analysis.pilot.charts import ANY_FROM

    return edge.from_value is not ANY_FROM


def _fmt_from(value: Any) -> str:
    """Format an edge from-value for reason strings — the ``ANY_FROM`` sentinel
    renders as ``'*'``, never as a raw ``<object object at 0x...>``."""
    from pyrung.core.analysis.pilot.charts import ANY_FROM

    return "*" if value is ANY_FROM else repr(value)


def _plan_ungrounded(plan: StaticPath) -> int:
    """1 when *plan*'s first edge rides a wildcard (``ANY_FROM``) from-value.

    A wildcard edge is a *stateless* claim — the writer's condition never named
    the channel register, so the graph learned no from-state for it.  A derived
    mask register (``statemask = table[StateRequested]``) produces exactly this
    shape: every edge wildcard, every plan one edge "long".  Ranking purely by
    edge count lets such a plan hijack the route: the 1-edge wildcard
    ``ANY --C_Start--> mask`` beats the real 3-edge Clear->Reset->Start chain on
    the state register, pressing Start from ABORTED where it is a no-op.  A plan
    grounded in a concrete from-value carries real distance information, so it
    outranks any wildcard-first plan.  Ordering only — a wildcard plan is still
    tried when nothing grounded exists.
    """
    return 0 if _edge_grounded(plan.first_edge) else 1


def _plan_off_target(plan: StaticPath, ctx: Any) -> int:
    """0 when *plan* drives the overall target, 1 otherwise.

    The trace can surface other requirements on the same channel register as
    the target — e.g. reaching ``S_StateCurrent==11`` (Held) trails
    ``==10`` (Holding, a real predecessor) and ``==1`` (Clearing, an off-path
    artifact of tracing the completion bool through a counterfactual writer).
    Ranking purely by edge count lets the shortest of these hijack the route: at
    Stopped the 3-edge ``C_Abort`` plan toward ``==1`` beats the 6-edge
    ``C_Reset`` plan toward the real target and drives the wrong way.  The
    target's own ``find_path`` already includes its required intermediate
    values, so anchor to it and let off-target requirement plans lose ties.
    """
    on_target = plan.needed_tag == ctx.target_tag and _values_match(
        plan.needed_value, ctx.target_value
    )
    return 0 if on_target else 1


def _route_plan_score(plan: StaticPath) -> tuple[int, int, str]:
    direct = 0 if plan.needed_tag == plan.role.channel_tag else 1
    return (len(plan.edges), direct, plan.role.channel_tag)


def _compass_route_actions(
    plan: StaticPath | None,
    frame: Any,
    ctx: Any,
    key_nogoods: set[_ActionPair],
) -> tuple[_ActionPair, ...]:
    if plan is None:
        return ()

    edge = plan.first_edge
    if edge.action is not None:
        if edge.action not in key_nogoods and ctx.route_allowed(edge.action):
            return (edge.action,)
        return ()

    direct: list[_ActionPair] = []
    for tag, value in edge.enablers:
        if _values_match(frame.snap.get(tag), value):
            continue
        pair = (tag, value)
        if tag in ctx.steerable and pair not in key_nogoods and ctx.route_allowed(pair):
            direct.append(pair)

    return tuple(direct)


def _oscillating_rungs(tag: str, ctx: Any, scope: Any, plc: Any) -> tuple[PilotRung, ...]:
    """A two-rule toggle for an edge-gated accumulator driver.

    Drives *tag* to each polarity while it sits at the other, so it alternates
    every scan — the rising/falling edge train the counter's pulse contract
    explicitly requests. This is option lowering of an owner-declared pulse,
    not a corrective behavior-category hypothesis.
    """
    from pyrung.core.condition import AllCondition, CompareNe

    resting = bool(ctx.resting.get(tag, False))
    other = not resting
    source = plc._known_tags_by_name[tag]
    return (
        PilotRung(tag, other, AllCondition(scope, CompareNe(source, other))),
        PilotRung(tag, resting, AllCondition(scope, CompareNe(source, resting))),
    )


def _managed_boolean_rungs(
    details: tuple[TraceAction, ...],
    frame: Any,
    state: Any,
    ctx: Any,
) -> tuple[tuple[PilotRung, ...], frozenset[_ActionPair]]:
    """Assert a rung-managed Boolean again under the new writer context.

    When an earlier guard expires, Boolean input-image lowering returns the input
    to False. If trace later needs that input again, it is already owned by
    PilotRungs, so a plain patch would lose to the overlay: append another rung
    guarded by the newly selected writer context. For ``rise(Input)`` only, add
    the input-polarity guard so the rung is a one-scan pulse; the False scan
    already happened naturally while no rung was active. A level prerequisite
    must remain asserted every scan while its context holds.
    """
    from pyrung.core.condition import AllCondition, CompareNe

    managed = {rung.dest for rung in state.rungs}
    overlay = _rung_execution_receipt(state.rungs, frame.snap)
    proposed: list[PilotRung] = []
    lowered: set[_ActionPair] = set()
    for detail in details:
        tag, value = detail.pair
        if (
            tag not in managed
            or type(value) is not bool
            or ((tag in ctx.edge_tags or detail.pulse) and value is not True)
        ):
            continue
        source = state.work._known_tags_by_name.get(tag)
        if source is None:
            continue
        if value is False:
            # The shared Boolean overlay already lowers every managed
            # destination to False before applying its active True rules. A
            # second False PilotRung duplicates that owner and, because it is
            # appended last, can override a later incident-scoped correction.
            # The current pulse/co-action still supplies the requested edge;
            # subsequent scans inherit the overlay's ordinary release value.
            lowered.add(detail.pair)
            continue
        active_owner = overlay.owner(tag)
        if active_owner is not None:
            # A replay-proved correction owns this input until its observed
            # release boundary. The backward trace is still diagnostic, but it
            # cannot append an opposite last-write-wins rung while that proof is
            # active. Once the guard is false, the fresh trace may steer it.
            lowered.add(detail.pair)
            continue
        if detail.guard_atoms:
            try:
                context = tuple(_atom_condition(state.work, atom) for atom in detail.guard_atoms)
            except (KeyError, ValueError):
                continue
        elif detail.until is not None:
            context = (_until_unresolved_condition(state.work, detail.until),)
        else:
            context = (
                _target_unresolved_condition(
                    state.work,
                    ctx.target_tag,
                    ctx.target_value,
                    ctx.target_predicate,
                ),
            )
        guard = (
            AllCondition(*context, CompareNe(source, value))
            if tag in ctx.edge_tags or detail.pulse
            else AllCondition(*context)
        )
        proposed.append(PilotRung(tag, value, guard))
        lowered.add(detail.pair)
    return tuple(proposed), frozenset(lowered)


# ---------------------------------------------------------------------------
# Candidate building — the compass in one call
# ---------------------------------------------------------------------------


def _current_bearing(frame: Any, ctx: Any) -> Any:
    """The program-owned current's operator action for the current state, or
    ``None``.

    Consulted only when the target register is an opaque-loop pipeline channel
    (the shape a program-owned command detour lives on).  Delegates to the
    read-side recognizer ``currents.current_readings`` over a
    ``WalkContext`` assembled from the live frame; fail-closed everywhere else.
    """
    channel = ctx.target_tag
    if channel not in ctx.opaque_loop:
        return None
    from pyrung.core.analysis.pilot.currents import (
        WorldView,
        current_readings,
        sibling_producer_family,
    )

    world = WorldView(
        snapshot=frame.snap,
        pdg=ctx.pdg,
        program=ctx.program,
        steerable=ctx.steerable,
        opaque_loop=ctx.opaque_loop,
        prior=ctx.domain_prior,
    )
    readings = current_readings(
        world,
        channel,
        ctx.pipeline_roles,
    )

    def _awaits_operator(reading: Any) -> bool:
        """Whether no automatic sibling already owns this command value.

        A current is an operator acknowledgement the program is waiting for.
        When the same command family has a program/environmental producer, its
        motion belongs to exact-producer preflight or an observed coast.  Using
        the equivalent operator button as a fallback would bypass that read and
        turn an ambient safety detour into a prescribed destination.
        """
        signature = reading.command_writes or ((reading.command_tag, reading.command_value),)
        automatic_owners = []
        for tag, value in signature:
            family = sibling_producer_family(world, tag, value)
            automatic_owners.append(
                family is not None
                and any(producer.kind != "operator" for producer in family.producers)
            )
        # A shared request strobe may have automatic writers while the command
        # discriminator remains operator-only. The automatic path subsumes the
        # push only when it owns every supplied command-gate component.
        return not automatic_owners or not all(automatic_owners)

    legal = tuple(
        reading
        for reading in readings
        if ctx.route_allowed(reading.action)
        and not _avoid_forces(ctx, [reading.action], frame.snap)
        and _awaits_operator(reading)
    )
    return legal[0] if len(legal) == 1 else None


def _completion_reread(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
) -> tuple[tuple[TraceAction, ...], tuple[_ActionPair, ...]]:
    """Re-trace a completion edge's charted gate pairs against the live world.

    The wait's bearing (``StaticTransitionEdge.completion`` — charts.py) is ordinary
    transparent ladder below the pipeline boundary: each recorded ``(tag,
    value)`` is traced back with a fresh walk (clean ancestry, so the
    opaque-loop feedback guard admits the one-hop descent), and its steerable
    leaves + unmet frontier are collected.  Availability orders, never rejects:
    a satisfied or self-advancing completion yields nothing pressable and the
    wait proceeds unchanged.  Returns ``(action_details, frontier)`` — the
    frontier lists the pressable levers (steerable leaves, e.g. ``x_RotateFB``)
    ahead of the trace's non-steerable interior so the terminal clause names the
    true blocker, not the post-cut interior.
    """
    details: list[TraceAction] = []
    seen_action: set[_ActionPair] = set()
    frontier: list[_ActionPair] = []
    seen_frontier: set[tuple[str, str]] = set()

    def _add_frontier(tag: str, value: Any) -> None:
        key = (tag, repr(value))
        if key not in seen_frontier:
            seen_frontier.add(key)
            frontier.append((tag, value))

    for tag, value in edge.completion:
        if _values_match(frame.snap.get(tag), value):
            continue
        tree = trace_back(
            tag,
            value,
            frame.snap,
            ctx.pdg,
            ctx.program,
            ctx.steerable,
            clear_only=ctx.clear_only,
            opaque_loop=ctx.opaque_loop,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            prior=ctx.domain_prior,
            avoid_pred=ctx.avoid_pred,
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
        for action in tree.ordered_action_details():
            if action.pair not in seen_action:
                seen_action.add(action.pair)
                details.append(action)
            if not _values_match(frame.snap.get(action.tag), action.value):
                _add_frontier(action.tag, action.value)
        for ftag, fval in frontier_pairs(tree, frame.snap):
            _add_frontier(ftag, fval)
    return tuple(details), tuple(frontier)


def _advance_heading(boundary: Any, frame: Any, state: Any) -> _ActionPair | None:
    """Lower an owned relational boundary to an exact observable heading."""
    from pyrung.core.analysis.pilot.advance import build_advance_index
    from pyrung.core.crossing import Cmp, Eq

    if isinstance(boundary, Eq) and len(boundary.values) == 1:
        return (boundary.tag, next(iter(boundary.values)))
    if not isinstance(boundary, Cmp) or boundary.op not in {">=", "<="}:
        return None
    owner = build_advance_index(
        state.work.program,
        getattr(state.work, "_harness", None),
    ).resolve(boundary.tag)
    if owner is None:
        return None
    target = frame.snap.get(str(boundary.bound)) if boundary.bound_is_tag else boundary.bound
    if target is None:
        return None
    return (boundary.tag, target)


def _prescribe_wait(
    edge: Any,
    frame: Any,
    state: Any,
    ctx: Any,
    *,
    reason: str | None = None,
) -> _WaitPrescription:
    """Mint a prescribed-wait bearing and re-read its charted completion.

    The single owner of "a wait is prescribed" for all three mint sites.  A
    route completion edge (the zoom / fallback sites) must be *grounded* — a
    wildcard from-value has no dwell semantics, so it refuses the wait (returns
    ``prescribed=False``).  Its charted ``completion`` pairs are re-read
    (:func:`_completion_reread`): the steerable leaves feed the candidate
    ranking and the unmet frontier names the true blocker.  An influence-path
    wait passes ``edge=None`` with an explicit ``reason`` — always coastable,
    nothing charted to re-read. Automatic sibling edges instead carry one
    exact-producer ``ProgramStep`` reading in the returned prescription.
    """
    if edge is None:
        return _WaitPrescription(True, reason)
    if not _edge_grounded(edge):
        return _WaitPrescription(False)

    route_reason = (
        f"let-run {edge.role.channel_tag}: {_fmt_from(edge.from_value)}->{edge.to_value!r}"
    )
    if edge.program_producers:
        from pyrung.core.analysis.pilot.currents import WorldView
        from pyrung.core.analysis.pilot.program_step import (
            ProgramStepStatus,
            read_program_step,
        )

        producers = tuple(
            {producer.rung_index: producer for producer in edge.program_producers}.values()
        )
        if len(producers) != 1:
            return _WaitPrescription(
                False,
                f"{route_reason}; exact program producer is ambiguous",
            )
        world = WorldView(
            snapshot=frame.snap,
            pdg=ctx.pdg,
            program=ctx.program,
            steerable=ctx.steerable,
            opaque_loop=ctx.opaque_loop,
            prior=ctx.domain_prior,
            clear_only=ctx.clear_only,
            pipeline_internal_tags=ctx.pipeline_internal_tags,
            pipeline_roles=ctx.pipeline_roles,
            avoid_pred=ctx.avoid_pred,
            via_pred=ctx.via_pred,
            harness=getattr(state.work, "_harness", None),
        )
        step = read_program_step(
            world,
            producers[0],
            state.work,
            state.rungs,
            resting=ctx.resting,
        )
        if step.status is ProgramStepStatus.KEEP_RUNNING:
            heading = _advance_heading(step.boundary, frame, state)
            if heading is None:
                return _WaitPrescription(
                    False,
                    f"{route_reason}; owned boundary has no exact coast heading",
                    program_step=step,
                )
            movement = next(
                (
                    (tag, before, after)
                    for tag, before, after in reversed(step.projected_changes)
                    if tag == step.channel and not _values_match(before, after)
                ),
                None,
            )
            observation = (
                f" ({movement[0]}: {movement[1]!r}->{movement[2]!r})"
                if movement is not None
                else f"; {step.reason}"
            )
            return _WaitPrescription(
                True,
                f"{route_reason}{observation}",
                frontier=(heading,),
                program_step=step,
                boundary=step.boundary,
            )
        if step.status is ProgramStepStatus.NEEDS_INPUT:
            handoff_by_action = {handoff.action: handoff for handoff in step.input_handoffs}
            if step.required_inputs and all(
                action.pair in handoff_by_action for action in step.required_inputs
            ):
                handoffs = tuple(handoff_by_action[action.pair] for action in step.required_inputs)
                boundary = handoffs[0].boundary
                if all(handoff.boundary == boundary for handoff in handoffs):
                    heading = _advance_heading(boundary, frame, state)
                    if heading is None:
                        return _WaitPrescription(
                            False,
                            f"{route_reason}; owned boundary has no exact coast heading",
                            details=step.required_inputs,
                            program_step=step,
                        )
                    # Compose the exact-producer read with the ordinary
                    # prerequisite/coast path. ``until`` gives each input the
                    # same scoped lifetime trace uses for any self-advancing
                    # operation; coast owns and verifies the resolved boundary.
                    details = tuple(
                        replace(
                            action,
                            until=handoff_by_action[action.pair].boundary,
                        )
                        for action in step.required_inputs
                    )
                    return _WaitPrescription(
                        True,
                        (f"{route_reason}; supply its current input and hand off to {heading[0]}"),
                        details=details,
                        frontier=(heading,),
                        program_step=step,
                        boundary=boundary,
                    )
            frontier = tuple(
                action.pair
                for action in step.required_inputs
                if action.pulse or not _values_match(frame.snap.get(action.tag), action.value)
            )
            return _WaitPrescription(
                False,
                f"{route_reason}; {step.reason}",
                details=step.required_inputs,
                frontier=frontier,
                program_step=step,
            )
        if step.status is ProgramStepStatus.INTERRUPTED:
            return _WaitPrescription(
                True,
                f"{route_reason}; {step.reason}",
                program_step=step,
            )
        return _WaitPrescription(
            False,
            f"{route_reason}; {step.reason}",
            program_step=step,
        )

    details, frontier = _completion_reread(edge, frame, state, ctx) if edge.completion else ((), ())
    return _WaitPrescription(True, route_reason, details, frontier)


def _rank_candidates(
    cands: list[_Candidate],
    frame: Any,
    ctx: Any,
    wake_cap: int,
    detail_by_pair: dict[_ActionPair, TraceAction],
) -> list[_Candidate]:
    """Score and sort candidates; records each rank rationale onto the candidate.

    A prescribed edge (route / influence / current) bypasses scoring and keeps
    top priority; every other candidate is ordered by worst-on-path writer
    availability, then wake, then compass progress.  ``index`` breaks every tie
    so the candidate object itself is never compared.
    """
    scored: list[tuple[tuple[int, int, int, int, int], int, _Candidate]] = []
    for index, candidate in enumerate(cands):
        established = (
            candidate.route_prescribed
            or candidate.influence_prescribed
            or candidate.current_prescribed
        )
        prescribed = established or candidate.program_prescribed
        prescription_tier = 0 if established else 1 if candidate.program_prescribed else 2
        base = (0, 0) if prescribed else _compass_score(candidate.pair, frame, ctx)
        avail_tier = 0 if prescribed else _availability_tier(detail_by_pair.get(candidate.pair))
        over_wake = (
            0 if prescribed else int(candidate.wake is not None and candidate.wake > wake_cap)
        )
        candidate = replace(
            candidate,
            avail_tier=avail_tier,
            over_wake=bool(over_wake),
            compass_score=(base[0], base[1]),
            scored=not prescribed,
        )
        scored.append(
            (
                (prescription_tier, avail_tier, over_wake, base[0], base[1]),
                index,
                candidate,
            )
        )
    return [candidate for _score, _index, candidate in sorted(scored)]


def _build_candidates(
    frame: Any,
    state: Any,
    ctx: Any,
) -> _CandidateList:
    key_nogoods = set(ctx.compass.knowledge.nogood_pairs(frame.key))
    # Clear-only (ack-cleared momentary) commands join the pulse-treatment set: the
    # program clears them every scan, so their idiom is pulse-and-release.  Holding
    # one steady as a prerequisite would assert a momentary command (a mode-change
    # request) forever — so they are pulsed like edge/action tags, never held.
    _act_or_edge = ctx.compass.action_tags | ctx.edge_tags | ctx.clear_only

    # Convergence command buttons currently held off-resting (and not a deliberate
    # forced hold).  A convergence pulse must release these or the program's
    # last-write-wins decoder fires the wrong command (a stuck CmdAbort overriding
    # CmdReset).  Empty for non-convergence programs → no effect.
    held_command_tags = frozenset(
        t
        for t in ctx.compass.action_tags
        if t not in {r.dest for r in state.rungs}
        and not _values_match(frame.snap.get(t), ctx.resting.get(t, False))
    )

    raw_detail_by_pair = {detail.pair: detail for detail in frame.raw_trace_action_details}
    active_trace_actions = tuple(
        pair
        for pair in frame.raw_trace_actions
        for detail in (raw_detail_by_pair.get(pair),)
        if pair not in ctx.blocked_route_actions
        and (
            not _values_match(frame.snap.get(pair[0]), pair[1])
            or pair[0] in ctx.edge_tags
            or (detail is not None and (detail.pulse or detail.until is not None))
        )
    )
    trace_actions = tuple(pair for pair in active_trace_actions if pair not in key_nogoods)
    detail_by_pair = {detail.pair: detail for detail in frame.raw_trace_action_details}
    trace_action_details = tuple(
        detail_by_pair[pair] for pair in trace_actions if pair in detail_by_pair
    )
    managed_boolean_rungs, lowered_rung_pairs = _managed_boolean_rungs(
        trace_action_details, frame, state, ctx
    )
    if lowered_rung_pairs:
        trace_actions = tuple(pair for pair in trace_actions if pair not in lowered_rung_pairs)
        active_trace_actions = tuple(
            pair for pair in active_trace_actions if pair not in lowered_rung_pairs
        )
        trace_action_details = tuple(
            detail for detail in trace_action_details if detail.pair not in lowered_rung_pairs
        )

    # Staged bearings: an ``establish`` action stands in stage 0 — it satisfies a
    # table-enablement precondition (the mode that unblocks a mask-disabled state)
    # whose effect is a settled cross-register recompute, so it cannot fire in the
    # same scan as the stage-1 command it gates.  While any establish action is
    # unsatisfied it is the *sole* bearing: the gated commands are deferred, and
    # the compass (route/influence) is silenced so nothing drives the target
    # register directly past the still-closed gate.  When the gate settles the
    # re-trace stops surfacing it and stage 1 becomes the bearing — no plan, no
    # done-check, just the lowest unmet stage.
    establish_details = tuple(d for d in trace_action_details if d.establish)
    establish_pending = bool(establish_details)
    deferred_commands: tuple[_ActionPair, ...] = ()
    if establish_pending:
        establish_pairs = {d.pair for d in establish_details}
        deferred_commands = tuple(p for p in trace_actions if p not in establish_pairs)
        trace_actions = tuple(p for p in trace_actions if p in establish_pairs)
        active_trace_actions = tuple(p for p in active_trace_actions if p in establish_pairs)
        trace_action_details = establish_details

    # Pending departure motion is only a fallback compass destination. A live
    # backward trace remains the stronger, more local bearing; this is what lets
    # PILOT finish work at the stopover before taking the proven return edge.
    live_plan = _compass_route_plan(frame, ctx, key_nogoods)
    if getattr(state, "pending_departure", None) is not None and active_trace_actions:
        route_plan = None
    else:
        route_plan = live_plan
    # A zoom iteration: the route's next edge is a completion (no action),
    # so the frontier self-advances under held state.  Prerequisites are the
    # level signals that must be held while timers accumulate.  A pending
    # establish gate is never a zoom: the frontier cannot self-advance past the
    # closed gate, so hold stage 0 as the bearing instead.
    _is_zoom = (
        not establish_pending and route_plan is not None and route_plan.first_edge.action is None
    )
    # An automatic chart edge is only a possible route until its exact program
    # producer is checked in this controlled world. If that producer offers no
    # executable wait or live input, remove just that edge from this read and
    # ask the same graph for the next route. This is how HELD walks around a
    # structurally possible Complete edge to the currently conductive Unhold
    # edge, without retaining a route suffix or poisoning another world.
    preflight_wait: _WaitPrescription | None = None
    excluded_edges: set[tuple[Any, ...]] = set()
    while _is_zoom:
        assert route_plan is not None
        preflight_wait = _prescribe_wait(route_plan.first_edge, frame, state, ctx)
        if (
            preflight_wait.prescribed
            or preflight_wait.details
            or not route_plan.first_edge.program_producers
        ):
            break
        excluded_edges.add(route_plan.first_edge.identity)
        alternate = _compass_route_plan(
            frame,
            ctx,
            key_nogoods,
            frozenset(excluded_edges),
        )
        if alternate is None:
            break
        route_plan = alternate
        preflight_wait = None
        _is_zoom = not establish_pending and route_plan.first_edge.action is None

    if _is_zoom or establish_pending:
        route_candidates: tuple[_ActionPair, ...] = ()
    else:
        route_candidates = _compass_route_actions(route_plan, frame, ctx, key_nogoods)
    # Co-actions for the route command (the one-shot edge gate); pulsed in the
    # same scan as the command candidate via _candidate_applied.
    route_co_actions: tuple[_ActionPair, ...] = (
        tuple(route_plan.first_edge.co_actions)
        if route_candidates and route_plan is not None
        else ()
    )

    wait_prescribed = False
    wait_reason: str | None = None
    completion_frontier: tuple[_ActionPair, ...] = ()
    program_step: Any = None
    program_pairs: set[_ActionPair] = set()
    advance_condition: Any = None
    # The next iteration re-reads the wait: when the zoom's grounded completion edge carries a
    # charted condition (charts.py), :func:`_prescribe_wait` re-traces it as
    # ordinary transparent ladder and returns its producers + unmet frontier.  The
    # producers enter the trace pool *before* the prerequisite split below, so a
    # self-advancing producer (``x_RotateFB``, ``until Rotate_Trans``) is held for
    # the coast and any remaining lever ranks as an ordinary candidate; the
    # frontier rides to the terminal clause via the frame so a stall names the true
    # blocker.  The wait's reason is applied at the mint site after scoring.
    _zoom_wait_prescribed = False
    _zoom_wait_reason: str | None = None
    if _is_zoom:
        assert route_plan is not None  # _is_zoom is True only when route_plan exists
        zoom_wait = preflight_wait or _prescribe_wait(route_plan.first_edge, frame, state, ctx)
        _zoom_wait_prescribed = zoom_wait.prescribed
        _zoom_wait_reason = zoom_wait.reason
        _comp_details = zoom_wait.details
        completion_frontier = zoom_wait.frontier
        program_step = zoom_wait.program_step
        advance_condition = zoom_wait.boundary
        if program_step is not None:
            program_pairs = {detail.pair for detail in program_step.required_inputs}
        for detail in _comp_details:
            pair = detail.pair
            existing_detail = detail_by_pair.get(pair)
            if (
                existing_detail is not None
                and existing_detail.until is None
                and detail.until is not None
            ):
                # The completion re-read may discover the lifetime that the
                # broader target trace could not see. Preserve the original
                # action evidence while composing in that owned boundary.
                detail_by_pair[pair] = replace(
                    existing_detail,
                    until=detail.until,
                )
            if (
                pair in active_trace_actions
                or pair in ctx.blocked_route_actions
                or pair in key_nogoods
                or not ctx.route_allowed(pair)
                or (
                    _values_match(frame.snap.get(pair[0]), pair[1]) and pair[0] not in ctx.edge_tags
                )
            ):
                continue
            detail_by_pair.setdefault(pair, detail)
            trace_actions = trace_actions + (pair,)
            active_trace_actions = active_trace_actions + (pair,)
            trace_action_details = trace_action_details + (detail,)

    # Prerequisite/command split: on zoom iterations, and on a self-advancing
    # coast leaf that has no compass route (a harness-linked sensor ramp, or a
    # timer/counter threshold reached via the terminal let-run rather than a
    # route zoom).  Prerequisites are non-action, non-edge steerable inputs that
    # must be *held* while the frontier self-advances — e.g. the Enable that
    # drives a sensor toward its threshold.  Without this they would be pulsed
    # and reverted as no-progress commands, and the coast would never ramp.  On
    # plain iterations, all trace actions are commands — pulse-and-judge.
    _is_coast = any(
        getattr(n, "advance", None) is not None and not n.satisfied for n in frame.tree.leaves()
    )
    advance_boundary: _ActionPair | None = (
        completion_frontier[0]
        if advance_condition is not None and len(completion_frontier) == 1
        else None
    )
    if _is_coast:
        for node in frame.tree.leaves():
            step = getattr(node, "advance", None)
            if step is None or node.satisfied:
                continue
            advance_boundary = (
                getattr(node, "owner_boundary", None)
                if getattr(node, "linear_boundary", False)
                else None
            ) or _advance_heading(step.until, frame, state)
            if advance_boundary is not None:
                advance_condition = (
                    getattr(node, "owner_condition", None)
                    if getattr(node, "linear_boundary", False)
                    else None
                ) or step.until
                break
    prerequisite_rungs = list(managed_boolean_rungs)
    if _is_zoom or _is_coast:
        # Edge-gated accumulator drivers (oscillate flag) toggle each scan via a
        # PilotRung instead of holding steady — a steady hold fires the edge
        # only once.  Routed as prerequisites so the terminal let-run animates and
        # records them; captured before the level loop because they are edge tags
        # the plain loop would otherwise leave as one-shot commands.
        # A steady hold that, held every scan, forces a rung writing a register the
        # tree still needs to a contradicting literal defeats the very frontier that
        # proposed it (``Heat_xInit=1`` forces ``fill(1, Heat_CurStep)`` while the
        # tree needs ``Heat_CurStep=3``).  Never install such a hold: skip it and
        # surface the skip.  Static, name-free (write-vs-need); belt-and-suspenders
        # on top of clear-only/writer-selection for levers those don't reroute.
        from pyrung.core.analysis.pilot.investigate import hold_defeats_needed

        needed = frontier_pairs(frame.tree, frame.snap)
        pulse_tags = {d.tag for d in trace_action_details if d.pulse}
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if tag in seen_prereq or tag in {r.dest for r in state.rungs}:
                continue
            detail = detail_by_pair.get((tag, value))
            if detail is None or detail.until is None:
                continue
            scope = _until_unresolved_condition(state.work, detail.until)
            if tag in pulse_tags:
                seen_prereq.add(tag)
                if ctx.route_allowed((tag, value)):
                    prerequisite_rungs.extend(_oscillating_rungs(tag, ctx, scope, state.work))
            elif tag not in _act_or_edge and not _values_match(frame.snap.get(tag), value):
                if hold_defeats_needed(tag, value, needed, ctx.pdg, ctx.program):
                    continue
                seen_prereq.add(tag)
                # Action gate for a prerequisite hold: a hold that drives an
                # avoided tag is a path that depends on it — never install it.
                if ctx.route_allowed((tag, value)) and not _avoid_forces(
                    ctx, [(tag, value)], frame.snap
                ):
                    prerequisite_rungs.append(PilotRung(tag, value, scope))
        prereq_tags = {r.dest for r in prerequisite_rungs}
        trace_actions = tuple(p for p in trace_actions if p[0] not in prereq_tags)
        active_trace_actions = tuple(p for p in active_trace_actions if p[0] not in prereq_tags)

    # Compass read: off-path masking and prescribed path from learned transitions.
    inf_candidates: list[_ActionPair] = []
    prescribed_action: _ActionPair | None = None
    prescribed_batch: tuple[_ActionPair, ...] | None = None
    probed_leaf_states: set[tuple[str, Any]] = set()
    # Stage 0 is the sole bearing while an establish gate is pending — silence the
    # compass so it can't prescribe a move on the target register past the closed
    # gate (or wait on a frontier that can't self-advance until the gate settles).
    for n in [] if establish_pending else _all_nodes(frame.tree):
        # Leaves only — with two "map unreadable here" exceptions where the
        # learned transition table is the only chart available: a live-guard
        # frontier (readable arm traced, so it has children, but the writer
        # guard is a live word), and a pipeline channel whose static value
        # graph produced NO plan (route_plan is None) but for which learned
        # transitions exist (skiff probes or route seeds).
        unreadable = getattr(n, "live_guard", False) or (
            getattr(n, "pipeline_internal", False)
            and route_plan is None
            and ctx.compass.has_transitions(n.tag, world_key=frame.key, snapshot=frame.snap)
        )
        if (
            (n.children and not unreadable)
            or n.satisfied
            or n.is_steerable
            or (getattr(n, "pipeline_internal", False) and not unreadable)
        ):
            continue
        cur_val = frame.snap.get(n.tag)
        if _values_match(cur_val, n.value):
            continue
        leaf_state = (n.tag, cur_val)
        if leaf_state in probed_leaf_states:
            continue
        probed_leaf_states.add(leaf_state)

        def _learned_edge_open(
            source: Any,
            cause: Any,
            destination: Any,
            *,
            tag: str = n.tag,
        ) -> bool:
            return _learned_edge_allowed(
                tag,
                source,
                cause,
                destination,
                frame,
                ctx,
                key_nogoods,
            )

        path = ctx.compass.find_path(
            n.tag,
            cur_val,
            n.value,
            cause_allowed=_learned_edge_open,
            world_key=frame.key,
            snapshot=frame.snap,
        )
        if path:
            first_step = path[0]
            if not is_action(first_step):
                influence_wait = _prescribe_wait(
                    None,
                    frame,
                    state,
                    ctx,
                    reason=f"{n.tag}: {cur_val!r}->{n.value!r}",
                )
                wait_prescribed = influence_wait.prescribed
                wait_reason = influence_wait.reason
                break
            if is_composite_action(first_step):
                # A skiff-learned joint edge: the whole action set must fire in
                # one window.  Propose it as a batch trial, not a single.
                # (The static type of a cause is a single action pair; a
                # composite is a tuple OF pairs, which is what the shape test
                # just established.)
                members = cast("tuple[_ActionPair, ...]", tuple(first_step))
                if all(pair not in key_nogoods and ctx.route_allowed(pair) for pair in members):
                    prescribed_batch = members
                    break
                continue
            if first_step not in key_nogoods and ctx.route_allowed(first_step):
                inf_candidates.append(first_step)
                prescribed_action = first_step
                break

    # Wake is an *ordering* input, never a filter.  An input with an
    # unusually large downstream write cone (a factory-reset call, or a master
    # enable feeding everything) poisons a batch and should be tried *last* — but
    # it must never be dropped, or a legitimately-needed lever with a large wake
    # makes the target silently unreachable.  Here we only split the
    # over-cap actions off the *batch-facing* ``trace_actions`` (widening /
    # convergence co-pulse) so they don't poison a batch trial; they are added
    # back as deprioritized individual candidates below and sink to the tail of
    # the ranked list via the ``over_wake`` sort dimension.
    wake_cap = 20
    over_wake_actions: tuple[_ActionPair, ...] = ()
    if len(trace_actions) > 1:
        radii = {t: len(ctx.pdg.downstream_slice(t, follow_calls=True)) for t, _v in trace_actions}
        median_r = sorted(radii.values())[len(radii) // 2] if radii else 0
        wake_cap = max(median_r * 3, 20)
        over_wake_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) > wake_cap)
        trace_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) <= wake_cap)

    candidates: list[_Candidate] = []
    seen_cand: set[_ActionPair] = set()
    route_candidate_set = set(route_candidates)

    def _candidate_for(pair: _ActionPair) -> _Candidate:
        detail = detail_by_pair.get(pair)
        prescribed_edge = (
            route_plan.first_edge
            if route_plan is not None and pair in route_candidate_set
            else None
        )
        return _Candidate(
            tag=pair[0],
            value=pair[1],
            influence_prescribed=prescribed_action is not None and pair == prescribed_action,
            provenance=detail.provenance if detail is not None else (),
            wake=(
                detail.wake
                if detail is not None and detail.wake is not None
                else len(ctx.pdg.downstream_slice(pair[0], follow_calls=True))
            ),
            route_prescribed=pair in route_candidate_set,
            bearing_channel_tag=(
                detail.operation_boundary[0]
                if detail is not None and detail.operation_boundary is not None
                else prescribed_edge.role.channel_tag
                if prescribed_edge is not None
                else route_plan.role.channel_tag
                if pair in program_pairs and route_plan is not None
                else None
            ),
            bearing_channel_value=(
                detail.operation_boundary[1]
                if detail is not None and detail.operation_boundary is not None
                else prescribed_edge.to_value
                if prescribed_edge is not None
                else route_plan.first_edge.to_value
                if pair in program_pairs and route_plan is not None
                else None
            ),
            program_prescribed=pair in program_pairs,
            program_note=(
                f"exact program producer rung {program_step.producer.rung_index} "
                f"currently needs {pair[0]}={pair[1]!r}"
                if pair in program_pairs and program_step is not None
                else ""
            ),
            program_context_actions=(
                tuple(program_step.context_actions)
                if pair in program_pairs and program_step is not None
                else ()
            ),
        )

    for pair in trace_actions:
        if pair not in ctx.blocked_route_actions and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in route_candidates:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in inf_candidates:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    # High-wake trace actions split off the batch above still get a turn as
    # individual candidates — the ``over_wake`` sort dimension below just files
    # them at the tail, so they are tried last rather than excluded outright.
    for pair in over_wake_actions:
        if pair not in ctx.blocked_route_actions and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    # Program-owned current: when the target register is an opaque-loop channel
    # whose backward trace dead-ends and whose compass route is the avoided
    # command, the trace surfaces no operator action for a program-owned detour
    # (the mid-recipe ack while HELD).  Recognize it directly — the one operator
    # push the program is dwelling on at the current state — and surface it as a
    # fallback bearing.  Fail-closed: only a *unique* legal, non-avoided push on a
    # recognized channel is returned; ambiguity / no-channel keep today's
    # behavior.  It appends *after* every read source, so a route/influence/trace
    # move keeps priority and this only matters where the loop is otherwise stuck.
    current_action = _current_bearing(frame, ctx)
    if current_action is not None:
        pair = current_action.action
        if (
            ctx.route_allowed(pair)
            and pair not in seen_cand
            and pair not in key_nogoods
            and (not _values_match(frame.snap.get(pair[0]), pair[1]) or pair[0] in ctx.edge_tags)
        ):
            seen_cand.add(pair)
            candidates.append(
                replace(
                    _candidate_for(pair),
                    current_prescribed=True,
                    current_note=current_action.note,
                    bearing_channel_tag=ctx.target_tag,
                    bearing_channel_value=current_action.to_state,
                )
            )
    # Writer-availability demotion (never veto): a command leaf whose writer chain
    # cannot fire from the current live state sinks below leaves whose chain is
    # reachable.  On a cyclic state machine every unsatisfied leaf across the
    # machine contributes a command candidate (C_Clear, C_Reset, C_Start,
    # mode-change… all at once); availability orders the counterfactual commands
    # below the ones actually reachable, ahead of the wake/compass tie-breakers.
    # Prescribed edges (the compass' explicit bearing) keep top priority.
    candidates = _rank_candidates(candidates, frame, ctx, wake_cap, detail_by_pair)

    # Zoom-wait mint: apply the reason from the early completion re-read (its
    # producers already entered the trace pool above).  An influence-path wait
    # (site 1, in the compass leaf loop) takes precedence when it fired.
    if _is_zoom and not wait_prescribed:
        wait_prescribed, wait_reason = _zoom_wait_prescribed, _zoom_wait_reason

    if advance_boundary is not None and not candidates and not wait_prescribed:
        wait_prescribed = True
        wait_reason = f"advance {advance_boundary[0]} to its next boundary {advance_boundary[1]!r}"

    # Fallback: route exists with an action but no candidates surfaced.  A wildcard
    # (ANY_FROM) edge whose action was filtered (nogooded / already at value) has
    # no dwell semantics — ``_prescribe_wait`` refuses it (the loop's stuck
    # diagnosis names the state instead of burning the budget on a dead register).
    if (
        route_plan is not None
        and not _is_zoom
        and not establish_pending
        and not route_candidates
        and not trace_actions
        and not wait_prescribed
    ):
        fallback_wait = _prescribe_wait(route_plan.first_edge, frame, state, ctx)
        wait_prescribed = fallback_wait.prescribed
        wait_reason = fallback_wait.reason
        completion_frontier += fallback_wait.frontier
        if fallback_wait.program_step is not None:
            program_step = fallback_wait.program_step
        if fallback_wait.boundary is not None:
            advance_condition = fallback_wait.boundary

    # Stuck diagnosis: no candidates from any reading source.  A skiff-learned
    # composite edge surfaces as ``prescribed_batch`` (a bearing, not a plan), and
    # a prescribed wait is an Act-tier bearing, so either means the loop has a move
    # to try -- not stuck.
    stuck_reason: str | None = None
    if (
        not candidates
        and not prerequisite_rungs
        and not wait_prescribed
        and prescribed_batch is None
    ):
        stuck_reason = _diagnose_stuck_reason(frame, ctx)

    return _CandidateList(
        active_trace_actions=active_trace_actions,
        trace_actions=trace_actions,
        trace_action_details=trace_action_details,
        route_candidates=route_candidates,
        candidates=tuple(candidates),
        wake_cap=wake_cap,
        route_plan=route_plan,
        wait_prescribed=wait_prescribed,
        wait_reason=wait_reason,
        advance_boundary=advance_boundary,
        advance_condition=advance_condition,
        prescribed_batch=prescribed_batch,
        prerequisite_rungs=tuple(prerequisite_rungs),
        stuck_reason=stuck_reason,
        route_co_actions=route_co_actions,
        deferred_commands=deferred_commands,
        held_command_tags=held_command_tags,
        completion_frontier=completion_frontier,
        program_step=program_step,
    )


# ---------------------------------------------------------------------------
# Pulse-action helpers
# ---------------------------------------------------------------------------


def _candidate_applied(
    candidate: _Candidate,
    candidates: _CandidateList,
    ctx: Any,
) -> tuple[_ActionPair, ...]:
    pair = candidate.pair
    actions: list[_ActionPair] = [pair]
    seen: set[str] = {pair[0]}

    # A route-prescribed command carries its co-actions (the one-shot edge gate);
    # they must fire in the same scan or the command rung never executes.
    if candidate.route_prescribed:
        for co in candidates.route_co_actions:
            if co[0] not in seen:
                actions.append(co)
                seen.add(co[0])

    if candidate.program_prescribed:
        for co in candidate.program_context_actions:
            # A pulse's own release/assert sequence is handled by _apply_pulse;
            # only independent context belongs in the atomic action set.
            if co[0] not in seen:
                actions.append(co)
                seen.add(co[0])

    # A convergence-pipeline command (CtrlCmd-style) co-pulses the remaining
    # trace actions so a level prerequisite and the command land together.
    if candidate.tag in ctx.compass.action_tags and candidates.active_trace_actions:
        # A pair rejected as a standalone act remains valid context for a
        # different atomic act.  Fresh orientation therefore keeps it out of
        # the candidate queue while still allowing the joint pulse to be
        # judged under its own Bearing identity.
        for ta in candidates.active_trace_actions:
            if ta[0] not in seen:
                actions.append(ta)
                seen.add(ta[0])

    # Releasing the other held convergence buttons is part of the same command:
    # without it the decoder fires a stuck button instead.  Recorded in the pulse
    # so replay reproduces a fully-specified, unambiguous command surface.
    if candidate.tag in ctx.compass.action_tags:
        for other in sorted(candidates.held_command_tags):
            if other not in seen:
                actions.append((other, ctx.resting.get(other, False)))
                seen.add(other)

    # Prerequisite holds (trace actions split into rungs for coast/zoom)
    # are applied to the fork but were removed from trace_actions — record them
    # so the scan_log faithfully captures everything the fork sees.
    for rung in candidates.prerequisite_rungs:
        tag, value = rung.dest, rung.value
        if tag not in seen:
            actions.append((tag, value))
            seen.add(tag)

    return tuple(actions)


def _co_actions(
    candidate: _Candidate,
    applied: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    return tuple(pair for pair in applied if pair != candidate.pair)
