"""Compass bearing → candidate list.

Reads the trace tree, route plans, influence paths, and upstream probes to
produce a ranked ``_CandidateList`` for the current iteration.  This is the
"compass" half of the loop — everything that decides *which way to steer*
before the pilot acts.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pilot.compass import is_action
from pyrung.core.analysis.pilot.steers import candidate_values_for_tag, upstream_candidates
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pilot.compass import Action, CompassPlan
    from pyrung.core.analysis.pilot.trace import TraceAction

_ActionPair = tuple[str, Any]
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

    @property
    def pair(self) -> _ActionPair:
        return (self.tag, self.value)


@dataclass(frozen=True)
class _CandidateList:
    active_trace_actions: tuple[_ActionPair, ...]
    trace_actions: tuple[_ActionPair, ...]
    trace_action_details: tuple[TraceAction, ...]
    route_candidates: tuple[_ActionPair, ...]
    upstream_candidates: tuple[_ActionPair, ...]
    influence_candidates: tuple[_ActionPair, ...]
    candidates: tuple[_Candidate, ...]
    blast_cap: int
    route_plan: CompassPlan | None = None
    wait_prescribed: bool = False
    wait_reason: str | None = None
    prerequisite_holds: tuple[_ActionPair, ...] = ()


# ---------------------------------------------------------------------------
# Tree traversal helper
# ---------------------------------------------------------------------------


def _all_nodes(tree: Any) -> list[Any]:
    """Collect all nodes in a TraceNode tree (breadth-first)."""
    result = [tree]
    i = 0
    while i < len(result):
        result.extend(result[i].children)
        i += 1
    return result


# ---------------------------------------------------------------------------
# Compass scoring / routing
# ---------------------------------------------------------------------------


def _compass_actions_for(
    tag: str,
    snap: dict[str, Any],
    ctx: Any,
    nogoods: set[_ActionPair],
) -> tuple[Action, ...]:
    action_tags = {
        action_tag
        for action_tag in ctx.pdg.upstream_slice(tag) & ctx.steerable & ctx.compass.action_tags
        if isinstance(action_tag, str) and action_tag in ctx.pdg.tags
    }
    actions: list[Action] = []
    for action_tag in sorted(action_tags):
        for value in candidate_values_for_tag(action_tag, snap, nogoods):
            action = (action_tag, value)
            if not ctx.route_allowed(action):
                continue
            actions.append(action)
    return tuple(actions)


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


def _compass_route_plan(
    frame: Any,
    ctx: Any,
) -> CompassPlan | None:
    if not ctx.compass.graphs:
        return None

    from pyrung.core.analysis.pilot.compass import best_compass_plan

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
    return min(plans, key=_route_plan_score)


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
    enabler_tags: set[str] = set()
    needed_values: dict[str, Any] = {}
    for tag, value in edge.enablers:
        if _values_match(frame.snap.get(tag), value):
            continue
        needed_values.setdefault(tag, value)
        enabler_tags.add(tag)
        pair = (tag, value)
        if tag in ctx.steerable and pair not in key_nogoods and ctx.route_allowed(pair):
            direct.append(pair)

    if direct:
        return tuple(direct)
    if not enabler_tags:
        return ()

    return tuple(
        pair
        for pair in upstream_candidates(
            enabler_tags,
            ctx.steerable,
            key_nogoods,
            frame.snap,
            ctx.pdg,
            nd_domains=ctx.nd_domains,
            needed_values=needed_values,
        )
        if ctx.route_allowed(pair)
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
    _act_or_edge = ctx.compass.action_tags | ctx.edge_tags

    active_trace_actions = tuple(
        (t, v)
        for t, v in frame.raw_trace_actions
        if (t, v) not in ctx.blocked_choice_actions
        and (not _values_match(frame.snap.get(t), v) or t in ctx.edge_tags)
    )
    trace_actions = tuple(pair for pair in active_trace_actions if pair not in key_nogoods)
    detail_by_pair = {detail.pair: detail for detail in frame.raw_trace_action_details}
    trace_action_details = tuple(
        detail_by_pair[pair] for pair in trace_actions if pair in detail_by_pair
    )

    # Always try to build route_plan — needed to detect timer-gated frontiers
    # even when the trace surfaces steerable leaves (feedbacks).
    route_plan = _compass_route_plan(frame, ctx)
    # A zoom iteration: the route's next edge is a completion (no action),
    # so the frontier self-advances under held state.  Prerequisites are the
    # level signals that must be held while timers accumulate.
    _is_zoom = route_plan is not None and route_plan.first_edge.action is None
    if _is_zoom:
        route_candidates: tuple[_ActionPair, ...] = ()
    else:
        route_candidates = _compass_route_actions(route_plan, frame, ctx, key_nogoods)

    stuck_tags = {
        n.tag
        for n in frame.tree.leaves()
        if (not n.satisfied and not n.is_steerable and not getattr(n, "pipeline_internal", False))
    }
    expanded_probe = stuck_tags | frame.tree.dead_end_parent_tags()
    needed_values: dict[str, Any] = {}
    for n in _all_nodes(frame.tree):
        if n.is_steerable and not n.satisfied and n.tag not in needed_values:
            needed_values[n.tag] = n.value
    up_candidates = tuple(
        upstream_candidates(
            expanded_probe,
            ctx.steerable,
            key_nogoods,
            frame.snap,
            ctx.pdg,
            nd_domains=ctx.nd_domains,
            needed_values=needed_values,
        )
    )

    # Prerequisite/command split: only on zoom iterations.
    # Prerequisites are non-action, non-edge steerable inputs that must be held
    # while a timer-gated frontier self-advances.  On non-zoom iterations, all
    # trace actions are commands — pulse-and-judge.
    # Prerequisite/command split: only trace-surfaced level signals, only on
    # zoom iterations.  Don't guess from upstream mining — the reactive path
    # (zoom → ejection → cause-chase → hold → retry) discovers what's missing.
    prerequisite_holds: list[_ActionPair] = []
    if _is_zoom:
        seen_prereq: set[str] = set()
        for tag, value in trace_actions:
            if (
                tag not in _act_or_edge
                and tag not in seen_prereq
                and tag not in state.forced_holds
                and not _values_match(frame.snap.get(tag), value)
            ):
                seen_prereq.add(tag)
                if ctx.route_allowed((tag, value)):
                    prerequisite_holds.append((tag, value))
        prereq_tags = {t for t, _ in prerequisite_holds}
        trace_actions = tuple(p for p in trace_actions if p[0] not in prereq_tags)
        active_trace_actions = tuple(p for p in active_trace_actions if p[0] not in prereq_tags)

    inf_candidates: list[_ActionPair] = []
    prescribed_action: Action | None = None
    wait_prescribed = False
    wait_reason: str | None = None
    probed_leaf_states: set[tuple[str, Any]] = set()
    for n in _all_nodes(frame.tree):
        if n.children or n.satisfied or n.is_steerable or getattr(n, "pipeline_internal", False):
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
                dbg(f"# influence masking off-path for {n.tag}: {sorted(route_off_path)}")

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
            if first_step not in key_nogoods and ctx.route_allowed(first_step):
                inf_candidates.append(first_step)
                prescribed_action = first_step
                dbg(f"# influence path for {n.tag}: {cur_val!r}->{n.value!r} = {path}")
                break
        if not ctx.compass.action_tags:
            continue
        available_actions = set(_compass_actions_for(n.tag, frame.snap, ctx, key_nogoods))
        new_probes = ctx.compass.unprobed_actions(n.tag, cur_val, available_actions)
        if new_probes:
            inf_candidates.extend(new_probes)
            dbg(f"# influence probing {n.tag} ({cur_val!r}->{n.value!r}): {new_probes}")
            break

    blast_cap = 20
    if len(trace_actions) > 1:
        radii = {t: len(ctx.pdg.downstream_slice(t, follow_calls=True)) for t, _v in trace_actions}
        median_r = sorted(radii.values())[len(radii) // 2] if radii else 0
        blast_cap = max(median_r * 3, 20)
        trace_actions = tuple((t, v) for t, v in trace_actions if radii.get(t, 0) <= blast_cap)

    candidates: list[_Candidate] = []
    broad: list[_Candidate] = []
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
        if pair not in ctx.blocked_choice_actions and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in route_candidates:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidates.append(_candidate_for(pair))
    for pair in [*inf_candidates, *up_candidates]:
        if ctx.route_allowed(pair) and pair not in seen_cand:
            seen_cand.add(pair)
            candidate = _candidate_for(pair)
            if len(ctx.pdg.downstream_slice(pair[0], follow_calls=True)) > blast_cap:
                broad.append(candidate)
            else:
                candidates.append(candidate)
    candidates.extend(broad)
    candidates = [
        candidate
        for _score, _index, candidate in sorted(
            (
                (
                    (
                        (0, 0)
                        if candidate.route_prescribed or candidate.influence_prescribed
                        else _compass_score(candidate.pair, frame, ctx)
                    ),
                    index,
                    candidate,
                )
                for index, candidate in enumerate(candidates)
            )
        )
    ]

    if ctx.debug:
        dbg(f"# trace_actions (filtered, {len(trace_actions)}): {list(trace_actions)}")
        dbg(f"# upstream_candidates ({len(up_candidates)}): blast_cap={blast_cap}")
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

    # Zoom iteration: route says the next step is a completion (WAIT).
    if _is_zoom and not wait_prescribed:
        edge = route_plan.first_edge  # type: ignore[union-attr]
        wait_prescribed = True
        wait_reason = (
            f"let-run {route_plan.role.governing_tag}: {edge.from_value!r}->{edge.to_value!r}"  # type: ignore[union-attr]
        )

    # Fallback: route exists with an action but no candidates surfaced.
    if (
        route_plan is not None
        and not _is_zoom
        and not route_candidates
        and not trace_actions
        and not wait_prescribed
    ):
        edge = route_plan.first_edge
        wait_prescribed = True
        wait_reason = (
            f"let-run {route_plan.role.governing_tag}: {edge.from_value!r}->{edge.to_value!r}"
        )

    return _CandidateList(
        active_trace_actions=active_trace_actions,
        trace_actions=trace_actions,
        trace_action_details=trace_action_details,
        route_candidates=route_candidates,
        upstream_candidates=up_candidates,
        influence_candidates=tuple(inf_candidates),
        candidates=tuple(candidates),
        blast_cap=blast_cap,
        route_plan=route_plan,
        wait_prescribed=wait_prescribed,
        wait_reason=wait_reason,
        prerequisite_holds=tuple(prerequisite_holds),
    )


# ---------------------------------------------------------------------------
# Pulse-action helpers
# ---------------------------------------------------------------------------


def _candidate_pulse_actions(
    candidate: _Candidate,
    candidates: _CandidateList,
    ctx: Any,
) -> tuple[_ActionPair, ...]:
    pair = candidate.pair
    if candidate.tag in ctx.compass.action_tags and candidates.trace_actions:
        return (
            pair,
            *((ta, tv) for ta, tv in candidates.trace_actions if ta != candidate.tag),
        )
    return (pair,)


def _context_actions(
    candidate: _Candidate,
    pulse_actions: tuple[_ActionPair, ...],
) -> tuple[_ActionPair, ...]:
    return tuple(pair for pair in pulse_actions if pair != candidate.pair)
