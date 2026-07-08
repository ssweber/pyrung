"""Compass bearing → candidate list.

Reads the trace tree and compass route plans to produce a ranked
``_CandidateList`` for the current iteration.  This is the "compass" half
of the loop — everything that decides *which way to steer* before the pilot
acts.

Only reading: trace-derived actions and statically-expanded compass routes.
No upstream cone mining, no influence probing — if the instruments can't
read the bearing, the pilot yields a stuck event and stops.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any, cast

from pyrung.core.analysis.pilot._ops import ConditionalHold, _avoid_forces, _HoldRule
from pyrung.core.analysis.pilot.compass import (
    _action_sort_key,
    is_action,
    is_composite_action,
)
from pyrung.core.analysis.pilot.trace import _all_nodes, _WriterAvailability
from pyrung.core.analysis.pilot.types import _ActionPair
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.statics import CompassPlan
    from pyrung.core.analysis.pilot.trace import TraceAction

_DebugFn = Callable[[str], None]


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Candidate:
    tag: str
    value: Any
    influence_prescribed: bool = False
    provenance: tuple[str, ...] = ()
    blast_radius: int | None = None
    route_prescribed: bool = False
    # Rank rationale — recorded at the scoring site (``_build_candidates``) and
    # surfaced through ``_candidate_payload`` so every candidate event carries why
    # it sorted where it did.  ``scored`` is False for a prescribed edge (the
    # compass' explicit bearing), which *bypasses* scoring: ``avail_tier`` /
    # ``over_blast`` / ``compass_score`` are then the forced (0, False, (0, 0))
    # bypass values, not measured ones.  ``None`` before scoring runs.
    avail_tier: int | None = None
    over_blast: bool | None = None
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
    blast_cap: int
    route_plan: CompassPlan | None = None
    wait_prescribed: bool = False
    wait_reason: str | None = None
    # A composite learned edge (skiff pair probe): the whole action set must
    # fire in one window.  Tried as a single batch trial before the singles —
    # verified live through the same gate pipeline as any candidate.
    prescribed_batch: tuple[_ActionPair, ...] | None = None
    prerequisite_holds: tuple[_ActionPair, ...] = ()
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
    if any(getattr(n, "self_advancing", False) and not n.satisfied for n in leaves):
        return None

    satisfied = [n for n in leaves if n.satisfied]
    if len(satisfied) == len(leaves) and leaves:
        return None

    # Writer found, all conditions satisfied (empty children) — the output
    # instruction just hasn't fired yet.  Let the loop coast one scan.
    if tree.writer_rung is not None and not tree.children:
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


def _compass_score(
    pair: _ActionPair,
    frame: Any,
    ctx: Any,
) -> tuple[int, int]:
    """Rank candidates by learned transition progress for current needs.

    Ordering, never rejection — the worst this returns is a high tier, so a
    backward move is tried last, not vetoed.  A known move of a *governing*
    register (``opaque_loop``) dominates: if the action drives a governing
    register away from the goal, it ranks backward even when it incidentally
    advances some lesser sub-need (steering ``C_Clear`` toward Stopped must not
    look like progress just because it ticks an unrelated flag).
    """
    gov_forward: tuple[int, int] | None = None
    gov_back: tuple[int, int] | None = None
    best_forward: tuple[int, int] | None = None
    best_regression: tuple[int, int] | None = None
    saw_known = False
    saw_no_change = False
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        cur_val = frame.snap.get(n.tag)
        if _values_match(cur_val, n.value):
            continue

        dest = ctx.compass.transition_dest(n.tag, cur_val, pair)
        if dest is None:
            if pair in ctx.compass.probed_actions(n.tag, cur_val):
                saw_no_change = True
            continue

        saw_known = True
        if _values_match(dest, n.value):
            score = (0, 0)
        else:
            forward = ctx.compass.find_path(n.tag, dest, n.value)
            if forward:
                score = (1, len(forward))
            else:
                back = ctx.compass.find_path(n.tag, dest, cur_val)
                if not back:
                    continue
                score = (150, len(back))

        backward = score[0] >= 150
        if n.tag in ctx.opaque_loop:
            if backward:
                gov_back = score if gov_back is None else min(gov_back, score)
            else:
                gov_forward = score if gov_forward is None else min(gov_forward, score)
        elif backward:
            best_regression = score if best_regression is None else min(best_regression, score)
        else:
            best_forward = score if best_forward is None else min(best_forward, score)

    if gov_forward is not None:
        return gov_forward
    if gov_back is not None:
        return gov_back
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
) -> CompassPlan | None:
    if not ctx.compass.graphs:
        return None

    from pyrung.core.analysis.pilot.statics import best_compass_plan

    plans: list[CompassPlan] = []
    for n in _all_nodes(frame.tree):
        if n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
            continue
        if not n.children and n.tag not in ctx.opaque_loop:
            continue
        if _values_match(frame.snap.get(n.tag), n.value):
            continue
        plan = best_compass_plan(n.tag, n.value, frame.snap, ctx.compass.graphs)
        if plan is not None:
            plans.append(plan)

    if not plans:
        return None
    return min(plans, key=lambda p: (_plan_off_target(p, ctx), _route_plan_score(p)))


def _plan_off_target(plan: CompassPlan, ctx: Any) -> int:
    """0 when *plan* drives the overall target, 1 otherwise.

    ``frame.tree`` surfaces waypoint sub-goals on the *same* governing register
    as the target — e.g. reaching ``S_StateCurrent==11`` (Held) trails
    ``==10`` (Holding, a real predecessor) and ``==1`` (Clearing, an off-path
    artifact of tracing the completion bool through a counterfactual writer).
    Ranking purely by edge count lets the shortest of these hijack the route: at
    Stopped the 3-edge ``C_Abort`` plan toward ``==1`` beats the 6-edge
    ``C_Reset`` plan toward the real target and drives the wrong way.  The
    target's own ``find_path`` already threads through the genuine waypoints, so
    anchor to it and let waypoint plans lose ties.
    """
    on_target = plan.needed_tag == ctx.target_tag and _values_match(
        plan.needed_value, ctx.target_value
    )
    return 0 if on_target else 1


def _route_plan_score(plan: CompassPlan) -> tuple[int, int, str]:
    direct = 0 if plan.needed_tag == plan.role.governing_tag else 1
    return (len(plan.edges), direct, plan.role.governing_tag)


def _compass_route_actions(
    plan: CompassPlan | None,
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


def _oscillating_hold(tag: str, ctx: Any) -> ConditionalHold:
    """A two-rule toggle for an edge-gated accumulator driver.

    Drives *tag* to each polarity while it sits at the other, so it alternates
    every scan — the rising/falling edge train the counter needs.  Mirrors the
    complement-reset OSCILLATE in ``corrections.py``; the terminal let-run
    animates it via the same ``ConditionalHold`` plumbing as the steady
    prerequisites.
    """
    resting = bool(ctx.resting.get(tag, False))
    other = not resting
    return ConditionalHold(
        rules=(
            _HoldRule(value=other, guard_tag=tag, guard_op="ne", guard_value=other),
            _HoldRule(value=resting, guard_tag=tag, guard_op="ne", guard_value=resting),
        )
    )


# ---------------------------------------------------------------------------
# Candidate building — the compass in one call
# ---------------------------------------------------------------------------


def _build_candidates(
    frame: Any,
    state: Any,
    ctx: Any,
    dbg: _DebugFn,
) -> _CandidateList:
    key_nogoods = state.nogoods.get(frame.key, set())
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
        if t not in state.forced_holds
        and not _values_match(frame.snap.get(t), ctx.resting.get(t, False))
    )

    active_trace_actions = tuple(
        (t, v)
        for t, v in frame.raw_trace_actions
        if (t, v) not in ctx.blocked_route_actions
        and (not _values_match(frame.snap.get(t), v) or t in ctx.edge_tags)
    )
    trace_actions = tuple(pair for pair in active_trace_actions if pair not in key_nogoods)
    detail_by_pair = {detail.pair: detail for detail in frame.raw_trace_action_details}
    trace_action_details = tuple(
        detail_by_pair[pair] for pair in trace_actions if pair in detail_by_pair
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

    # Always try to build route_plan — needed to detect timer-gated frontiers
    # even when the trace surfaces steerable leaves (feedbacks).
    route_plan = _compass_route_plan(frame, ctx)
    # A zoom iteration: the route's next edge is a completion (no action),
    # so the frontier self-advances under held state.  Prerequisites are the
    # level signals that must be held while timers accumulate.  A pending
    # establish gate is never a zoom: the frontier cannot self-advance past the
    # closed gate, so hold stage 0 as the bearing instead.
    _is_zoom = (
        not establish_pending and route_plan is not None and route_plan.first_edge.action is None
    )
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

    # Prerequisite/command split: on zoom iterations, and on a self-advancing
    # coast leaf that has no compass route (a harness-linked sensor ramp, or a
    # timer/counter threshold reached via the terminal let-run rather than a
    # route zoom).  Prerequisites are non-action, non-edge steerable inputs that
    # must be *held* while the frontier self-advances — e.g. the Enable that
    # drives a sensor toward its threshold.  Without this they would be pulsed
    # and reverted as no-progress commands, and the coast would never ramp.  On
    # plain iterations, all trace actions are commands — pulse-and-judge.
    _is_coast = any(
        getattr(n, "self_advancing", False) and not n.satisfied for n in frame.tree.leaves()
    )
    prerequisite_holds: list[_ActionPair] = []
    if _is_zoom or _is_coast:
        # Edge-gated accumulator drivers (oscillate flag) toggle each scan via a
        # ConditionalHold instead of holding steady — a steady hold fires the edge
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
        from pyrung.core.analysis.pilot.trace import frontier_pairs

        needed = frontier_pairs(frame.tree, frame.snap)
        oscillate_tags = {d.tag for d in trace_action_details if d.oscillate}
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if tag in seen_prereq or tag in state.forced_holds:
                continue
            if tag in oscillate_tags:
                seen_prereq.add(tag)
                if ctx.route_allowed((tag, value)):
                    prerequisite_holds.append((tag, _oscillating_hold(tag, ctx)))
            elif tag not in _act_or_edge and not _values_match(frame.snap.get(tag), value):
                if hold_defeats_needed(tag, value, needed, ctx.pdg, ctx.program):
                    dbg(
                        f"# skip self-defeating hold {tag}={value!r}: forces a write "
                        f"contradicting needed {list(needed)}"
                    )
                    continue
                seen_prereq.add(tag)
                # Action gate for a prerequisite hold: a hold that drives an
                # avoided tag is a path that depends on it — never install it.
                if ctx.route_allowed((tag, value)) and not _avoid_forces(
                    ctx, [(tag, value)], frame.snap
                ):
                    prerequisite_holds.append((tag, value))
        prereq_tags = {t for t, _ in prerequisite_holds}
        trace_actions = tuple(p for p in trace_actions if p[0] not in prereq_tags)
        active_trace_actions = tuple(p for p in active_trace_actions if p[0] not in prereq_tags)

    # Compass read: off-path masking and prescribed path from learned transitions.
    inf_candidates: list[_ActionPair] = []
    prescribed_action: _ActionPair | None = None
    prescribed_batch: tuple[_ActionPair, ...] | None = None
    wait_prescribed = False
    wait_reason: str | None = None
    probed_leaf_states: set[tuple[str, Any]] = set()
    # Stage 0 is the sole bearing while an establish gate is pending — silence the
    # compass so it can't prescribe a move on the target register past the closed
    # gate (or wait on a frontier that can't self-advance until the gate settles).
    for n in [] if establish_pending else _all_nodes(frame.tree):
        # Leaves only — with two "map unreadable here" exceptions where the
        # learned transition table is the only chart available: a live-guard
        # frontier (readable arm traced, so it has children, but the writer
        # guard is a live word), and a pipeline governor whose static value
        # graph produced NO plan (route_plan is None) but for which learned
        # transitions exist (skiff probes or route seeds).
        unreadable = getattr(n, "live_guard", False) or (
            getattr(n, "pipeline_internal", False)
            and route_plan is None
            and ctx.compass.has_transitions(n.tag)
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

        off_path = ctx.compass.off_path_actions(n.tag, cur_val, n.value)
        if off_path:
            route_off_path = {action for action in off_path if ctx.route_allowed(action)}
            state.nogoods.setdefault(frame.key, set()).update(route_off_path)
            key_nogoods = state.nogoods.get(frame.key, set())
            if route_off_path:
                dbg(
                    "# influence masking off-path for "
                    f"{n.tag}: {sorted(route_off_path, key=_action_sort_key)}"
                )

        path = ctx.compass.find_path(n.tag, cur_val, n.value)
        if path:
            first_step = path[0]
            if not is_action(first_step):
                wait_prescribed = True
                wait_reason = f"{n.tag}: {cur_val!r}->{n.value!r}"
                dbg(
                    f"# influence path for {n.tag}: {cur_val!r}->{n.value!r} "
                    f"begins with {first_step}"
                )
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
                    dbg(f"# influence batch for {n.tag}: {cur_val!r}->{n.value!r} = {members}")
                    break
                continue
            if first_step not in key_nogoods and ctx.route_allowed(first_step):
                inf_candidates.append(first_step)
                prescribed_action = first_step
                dbg(f"# influence path for {n.tag}: {cur_val!r}->{n.value!r} = {path}")
                break

    # Blast radius is an *ordering* input, never a filter.  An input with an
    # unusually large downstream write cone (a factory-reset call, or a master
    # enable feeding everything) poisons a batch and should be tried *last* — but
    # it must never be dropped, or a legitimately-needed lever with a large blast
    # radius makes the target silently unreachable.  Here we only split the
    # over-cap actions off the *batch-facing* ``trace_actions`` (widening /
    # convergence co-pulse) so they don't poison a batch trial; they are added
    # back as deprioritized individual candidates below and sink to the tail of
    # the ranked list via the ``over_blast`` sort dimension.
    blast_cap = 20
    over_blast_actions: tuple[_ActionPair, ...] = ()
    if len(trace_actions) > 1:
        radii = {t: len(ctx.pdg.downstream_slice(t, follow_calls=True)) for t, _v in trace_actions}
        median_r = sorted(radii.values())[len(radii) // 2] if radii else 0
        blast_cap = max(median_r * 3, 20)
        over_blast_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) > blast_cap)
        trace_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) <= blast_cap)

    candidates: list[_Candidate] = []
    seen_cand: set[_ActionPair] = set()
    route_candidate_set = set(route_candidates)

    def _candidate_for(pair: _ActionPair) -> _Candidate:
        detail = detail_by_pair.get(pair)
        return _Candidate(
            tag=pair[0],
            value=pair[1],
            influence_prescribed=prescribed_action is not None and pair == prescribed_action,
            provenance=detail.provenance if detail is not None else (),
            blast_radius=(
                detail.blast_radius
                if detail is not None and detail.blast_radius is not None
                else len(ctx.pdg.downstream_slice(pair[0], follow_calls=True))
            ),
            route_prescribed=pair in route_candidate_set,
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
    # High-blast trace actions split off the batch above still get a turn as
    # individual candidates — the ``over_blast`` sort dimension below just files
    # them at the tail, so they are tried last rather than excluded outright.
    for pair in over_blast_actions:
        if pair not in ctx.blocked_route_actions and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    scored: list[tuple[tuple[int, int, int, int], int, _Candidate]] = []
    for index, candidate in enumerate(candidates):
        prescribed = candidate.route_prescribed or candidate.influence_prescribed
        base = (0, 0) if prescribed else _compass_score(candidate.pair, frame, ctx)
        # Writer-availability demotion (never veto): a command leaf whose writer
        # chain cannot fire from the current live state sinks below leaves whose
        # chain is reachable.  On a cyclic state machine every unsatisfied leaf
        # across the machine contributes a command candidate (C_Clear, C_Reset,
        # C_Start, mode-change… all at once); ordering by the worst-on-path writer
        # availability sinks the counterfactual commands below the ones actually
        # reachable from here, ahead of the blast/compass tie-breakers.  Prescribed
        # edges (the compass' explicit bearing) keep top priority regardless.
        avail_tier = 0 if prescribed else _availability_tier(detail_by_pair.get(candidate.pair))
        # Deprioritize (never veto) a candidate whose downstream write cone
        # exceeds the cap: it sorts into a trailing tier so a large-blast master
        # enable is tried after every tighter lever.  Prescribed edges (the
        # compass' explicit bearing) keep their priority regardless of blast.
        over_blast = (
            0
            if prescribed
            else int(candidate.blast_radius is not None and candidate.blast_radius > blast_cap)
        )
        # Record the rank rationale onto the candidate (recording only — the sort
        # key below is byte-identical, and ``index`` breaks every tie so the
        # candidate object itself is never compared).
        candidate = replace(
            candidate,
            avail_tier=avail_tier,
            over_blast=bool(over_blast),
            compass_score=(base[0], base[1]),
            scored=not prescribed,
        )
        scored.append(((avail_tier, over_blast, base[0], base[1]), index, candidate))
    candidates = [candidate for _score, _index, candidate in sorted(scored)]

    # Zoom iteration: route says the next step is a completion (WAIT).
    if _is_zoom and not wait_prescribed:
        assert route_plan is not None  # _is_zoom is True only when route_plan exists
        edge = route_plan.first_edge
        wait_prescribed = True
        wait_reason = (
            f"let-run {route_plan.role.governing_tag}: {edge.from_value!r}->{edge.to_value!r}"
        )

    # Fallback: route exists with an action but no candidates surfaced.
    if (
        route_plan is not None
        and not _is_zoom
        and not establish_pending
        and not route_candidates
        and not trace_actions
        and not wait_prescribed
    ):
        edge = route_plan.first_edge
        wait_prescribed = True
        wait_reason = (
            f"let-run {route_plan.role.governing_tag}: {edge.from_value!r}->{edge.to_value!r}"
        )

    # Stuck diagnosis: no candidates from any reading source.  A skiff-learned
    # composite edge surfaces as ``prescribed_batch`` (a bearing, not a plan), and
    # a prescribed wait is an Act-tier bearing, so either means the loop has a move
    # to try -- not stuck.
    stuck_reason: str | None = None
    if not candidates and not wait_prescribed and prescribed_batch is None:
        stuck_reason = _diagnose_stuck_reason(frame, ctx)

    if ctx.debug:
        dbg(f"# trace_actions (filtered, {len(trace_actions)}): {list(trace_actions)}")
        if prerequisite_holds:
            dbg(f"# prerequisite_holds ({len(prerequisite_holds)}): {prerequisite_holds}")
        if route_candidates:
            edge = route_plan.first_edge if route_plan is not None else None
            dbg(
                "# route_candidates "
                f"({len(route_candidates)}): {route_candidates}"
                + (
                    ""
                    if edge is None
                    else f" via {edge.role.governing_tag}: {edge.from_value!r}->{edge.to_value!r}"
                )
            )
        if inf_candidates:
            dbg(f"# influence_candidates ({len(inf_candidates)}): {inf_candidates}")
        if wait_prescribed:
            dbg(f"# influence_wait: {wait_reason}")
        if stuck_reason:
            dbg(f"# stuck: {stuck_reason}")

    return _CandidateList(
        active_trace_actions=active_trace_actions,
        trace_actions=trace_actions,
        trace_action_details=trace_action_details,
        route_candidates=route_candidates,
        candidates=tuple(candidates),
        blast_cap=blast_cap,
        route_plan=route_plan,
        wait_prescribed=wait_prescribed,
        wait_reason=wait_reason,
        prescribed_batch=prescribed_batch,
        prerequisite_holds=tuple(prerequisite_holds),
        stuck_reason=stuck_reason,
        route_co_actions=route_co_actions,
        deferred_commands=deferred_commands,
        held_command_tags=held_command_tags,
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

    # A convergence-pipeline command (CtrlCmd-style) co-pulses the remaining
    # trace actions so a level prerequisite and the command land together.
    if candidate.tag in ctx.compass.action_tags and candidates.trace_actions:
        for ta in candidates.trace_actions:
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

    # Prerequisite holds (trace actions split into forced_holds for coast/zoom)
    # are applied to the fork but were removed from trace_actions — record them
    # so the scan_log faithfully captures everything the fork sees.
    for tag, value in candidates.prerequisite_holds:
        if tag not in seen:
            actions.append((tag, value))
            seen.add(tag)

    return tuple(actions)


def _co_actions(
    candidate: _Candidate,
    applied: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    return tuple(pair for pair in applied if pair != candidate.pair)
