"""Waypoint planner for explore-less how().

Decomposes a target condition into intermediate waypoints (stateful tags
that must change value), orders them by dependency, and runs a scoped
mini-BFS per waypoint.  Falls back to undecomposed BFS when decomposition
fails or any mini-BFS exhausts its budget.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from pyrung.core.analysis.pdg import ProgramGraph, TagRole
from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or


@dataclass(frozen=True)
class _Waypoint:
    tag_name: str
    required_value: Any
    cone: frozenset[str]


def _extract_required_values(
    expr: Expr,
    snapshot: dict[str, Any],
) -> list[tuple[str, Any]] | None:
    """Extract ``(tag_name, required_value)`` pairs from *expr*.

    Returns ``None`` when the expression contains forms that cannot be
    inverted to a concrete required value (rise/fall/truthy, complex
    disjunctions where branch selection isn't obvious).
    """
    if isinstance(expr, Const):
        return []

    if isinstance(expr, Atom):
        return _required_from_atom(expr)

    if isinstance(expr, And):
        pairs: list[tuple[str, Any]] = []
        for term in expr.terms:
            sub = _extract_required_values(term, snapshot)
            if sub is None:
                return None
            pairs.extend(sub)
        return pairs

    if isinstance(expr, Or):
        best: list[tuple[str, Any]] | None = None
        best_cost = float("inf")
        for term in expr.terms:
            sub = _extract_required_values(term, snapshot)
            if sub is None:
                continue
            cost = sum(1 for tag, val in sub if snapshot.get(tag) != val)
            if cost < best_cost:
                best = sub
                best_cost = cost
        return best

    return None


def _required_from_atom(atom: Atom) -> list[tuple[str, Any]] | None:
    form = atom.form
    if form == "xic":
        return [(atom.tag, True)]
    if form == "xio":
        return [(atom.tag, False)]
    if form == "eq":
        return [(atom.tag, atom.operand)]
    if form in {"rise", "fall", "truthy"}:
        return None
    if form == "ne":
        return None
    if form in {"lt", "le", "gt", "ge"}:
        return None
    return None


def _discover_waypoints(
    snapshot: dict[str, Any],
    target_expr: Expr,
    pdg: ProgramGraph,
    program: Any,
) -> list[_Waypoint] | None:
    """Find stateful tags that must change to satisfy *target_expr*.

    Returns ``None`` when the expression cannot be decomposed into waypoints.
    """
    required = _extract_required_values(target_expr, snapshot)
    if required is None:
        return None

    unsatisfied = [(tag, val) for tag, val in required if snapshot.get(tag) != val]
    if not unsatisfied:
        return []

    from pyrung.core.analysis.causal.support import _collect_sp_leaves
    from pyrung.core.analysis.reverse_edges import (
        back_propagate_value,
        build_reverse_edge_map,
    )
    from pyrung.core.analysis.simplified import _sp_to_expr

    edge_map = build_reverse_edge_map(program)

    waypoints: list[_Waypoint] = []
    seen: set[str] = set()
    queue: deque[tuple[str, Any]] = deque(unsatisfied)

    while queue:
        tag_name, required_value = queue.popleft()
        if tag_name in seen:
            continue
        seen.add(tag_name)

        if tag_name not in pdg.writers_of:
            continue

        role = pdg.tag_roles.get(tag_name)
        if role == TagRole.INPUT:
            continue

        cone = pdg.upstream_slice(tag_name)
        waypoints.append(_Waypoint(tag_name, required_value, cone))

        propagated = back_propagate_value(edge_map, tag_name, required_value)
        for src_tag, src_val in propagated.items():
            if src_tag not in seen and snapshot.get(src_tag) != src_val:
                queue.append((src_tag, src_val))

        logic = program.rungs if hasattr(program, "rungs") else program
        for rung_idx in pdg.writers_of[tag_name]:
            node = pdg.rung_nodes[rung_idx]
            if node.rung_index >= len(logic):
                continue
            rung = logic[node.rung_index]
            for bi in node.branch_path:
                rung = rung._branches[bi]
            sp_tree = rung.sp_tree()
            if sp_tree is None:
                continue
            for leaf in _collect_sp_leaves(sp_tree):
                leaf_expr = _sp_to_expr(leaf) if hasattr(leaf, "children") else None
                if leaf_expr is None:
                    cond = getattr(leaf, "condition", None)
                    if cond is None:
                        continue
                    from pyrung.core.analysis.simplified import _condition_to_expr

                    leaf_expr = _condition_to_expr(cond)
                leaf_pairs = _extract_required_values(leaf_expr, snapshot)
                if leaf_pairs is None:
                    continue
                for lt, lv in leaf_pairs:
                    if lt not in seen and snapshot.get(lt) != lv:
                        if lt in pdg.writers_of and pdg.tag_roles.get(lt) != TagRole.INPUT:
                            queue.append((lt, lv))

    return waypoints


def _order_waypoints(
    waypoints: list[_Waypoint],
    pdg: ProgramGraph,
) -> list[_Waypoint] | None:
    """Topological sort of waypoints by condition-reads dependency.

    Returns ``None`` on cycles (fall back to undecomposed BFS).
    """
    if len(waypoints) <= 1:
        return waypoints

    wp_tags = {wp.tag_name for wp in waypoints}
    wp_by_tag = {wp.tag_name: wp for wp in waypoints}

    deps: dict[str, set[str]] = {wp.tag_name: set() for wp in waypoints}
    for wp in waypoints:
        for rung_idx in pdg.writers_of.get(wp.tag_name, frozenset()):
            node = pdg.rung_nodes[rung_idx]
            for read_tag in node.condition_reads:
                if read_tag in wp_tags and read_tag != wp.tag_name:
                    deps[wp.tag_name].add(read_tag)

    in_degree = {t: len(d) for t, d in deps.items()}
    ready: deque[str] = deque(t for t, d in in_degree.items() if d == 0)
    ordered: list[_Waypoint] = []

    while ready:
        tag = ready.popleft()
        ordered.append(wp_by_tag[tag])
        for other, other_deps in deps.items():
            if tag in other_deps:
                other_deps.discard(tag)
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    ready.append(other)

    if len(ordered) != len(waypoints):
        return None

    return ordered


def _run_waypoint_plan(
    waypoints: list[_Waypoint],
    snapshot: dict[str, Any],
    target_pred: Any,
    program: Any,
    max_steps: int,
    opt: Any,
    compiled: Any = None,
) -> list[Any] | None:
    """Execute scoped mini-BFS per waypoint.

    Returns the combined trace steps on success, or ``None`` to signal
    fallback to undecomposed BFS.
    """
    from pyrung.core.analysis.prove import _build_explore_context
    from pyrung.core.analysis.prove.bfs import _bfs_explore_gen
    from pyrung.core.analysis.prove.results import Counterexample, Intractable

    if not waypoints:
        return None

    budget_per_wp = max(5, max_steps // (len(waypoints) + 1))
    current_state = dict(snapshot)
    all_trace_steps: list[Any] = []

    wp_generators: list[Any] = []

    for i, wp in enumerate(waypoints):
        wp_pred = _make_wp_predicate(wp)
        scope = sorted(wp.cone | {wp.tag_name})

        context = _build_explore_context(
            program,
            scope=scope,
            _opt_config=opt,
            compiled=compiled,
            initial_state=current_state,
        )
        if isinstance(context, Intractable):
            return None

        gen = _bfs_explore_gen(
            context,
            predicates=[lambda s, _p=wp_pred: not _p(s)],
            depth_budget=budget_per_wp,
            max_states=10_000,
            bfs_config=opt.bfs_config,
            initial_state=current_state,
        )

        result_list = next(gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            # Backtrack: try resuming previous waypoint generators
            recovered = _backtrack(
                wp_generators,
                i,
                waypoints,
                current_state,
                snapshot,
                all_trace_steps,
                program,
                opt,
                budget_per_wp,
                target_pred,
                compiled=compiled,
            )
            if recovered is not None:
                return recovered
            return None

        trace = result_list[0].trace
        wp_generators.append((gen, context, len(all_trace_steps)))

        new_state = _replay_to_state(context.compiled, current_state, trace)
        current_state = new_state
        all_trace_steps.extend(trace)

    return all_trace_steps


def _make_wp_predicate(wp: _Waypoint) -> Any:
    def pred(state: dict[str, Any]) -> bool:
        return state.get(wp.tag_name) == wp.required_value

    return pred


def _replay_to_state(
    compiled: Any,
    snapshot: dict[str, Any],
    trace: list[Any],
) -> dict[str, Any]:
    """Replay a trace and return the final tag state."""
    from pyrung.core.analysis.prove.kernel import _step_compiled_kernel

    kernel = compiled.create_kernel()
    for n, v in snapshot.items():
        if n in kernel.tags:
            kernel.tags[n] = v
    for step in trace:
        if not step.inputs and step.scans == 0:
            continue
        for n, v in step.inputs.items():
            kernel.tags[n] = v
        for _ in range(step.scans):
            _step_compiled_kernel(compiled, kernel, dt=0.010)
    return dict(kernel.tags)


def _backtrack(
    wp_generators: list[Any],
    failed_idx: int,
    waypoints: list[Any],
    current_state: dict[str, Any],
    original_snapshot: dict[str, Any],
    all_trace_steps: list[Any],
    program: Any,
    opt: Any,
    budget_per_wp: int,
    target_pred: Any,
    max_retries: int = 3,
    compiled: Any = None,
) -> list[Any] | None:
    """Try resuming a previous waypoint's generator for an alternate path.

    Returns the combined trace on success, or ``None`` on exhaustion.
    """
    from pyrung.core.analysis.prove.results import Counterexample

    for _attempt in range(max_retries):
        if not wp_generators:
            return None

        prev_gen, prev_context, trace_start = wp_generators[-1]
        wp_generators.pop()

        result_list = next(prev_gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            continue

        prev_wp_idx = len(wp_generators)
        prev_trace = result_list[0].trace

        steps_before = all_trace_steps[:trace_start]

        state_before = dict(original_snapshot)
        for step in steps_before:
            if not step.inputs and step.scans == 0:
                continue
        if trace_start > 0:
            state_before = _replay_to_state(prev_context.compiled, original_snapshot, steps_before)

        new_state = _replay_to_state(prev_context.compiled, state_before, prev_trace)
        new_trace = list(steps_before) + list(prev_trace)
        wp_generators.append((prev_gen, prev_context, trace_start))

        remaining = waypoints[prev_wp_idx + 1 :]
        sub_result = _run_remaining_waypoints(
            remaining,
            new_state,
            new_trace,
            program,
            opt,
            budget_per_wp,
            wp_generators,
            waypoints,
            original_snapshot,
            target_pred,
            compiled=compiled,
        )
        if sub_result is not None:
            return sub_result

    return None


def _run_remaining_waypoints(
    remaining: list[Any],
    current_state: dict[str, Any],
    trace_so_far: list[Any],
    program: Any,
    opt: Any,
    budget_per_wp: int,
    wp_generators: list[Any],
    all_waypoints: list[Any],
    original_snapshot: dict[str, Any],
    target_pred: Any,
    compiled: Any = None,
) -> list[Any] | None:
    """Try to complete the remaining waypoints from a new state."""
    from pyrung.core.analysis.prove import _build_explore_context
    from pyrung.core.analysis.prove.bfs import _bfs_explore_gen
    from pyrung.core.analysis.prove.results import Counterexample, Intractable

    all_trace = list(trace_so_far)
    state = dict(current_state)

    for wp in remaining:
        wp_pred = _make_wp_predicate(wp)

        if wp_pred(state):
            continue

        scope = sorted(wp.cone | {wp.tag_name})
        context = _build_explore_context(
            program,
            scope=scope,
            _opt_config=opt,
            compiled=compiled,
            initial_state=state,
        )
        if isinstance(context, Intractable):
            return None

        gen = _bfs_explore_gen(
            context,
            predicates=[lambda s, _p=wp_pred: not _p(s)],
            depth_budget=budget_per_wp,
            max_states=10_000,
            bfs_config=opt.bfs_config,
            initial_state=state,
        )

        result_list = next(gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            return None

        trace = result_list[0].trace
        new_state = _replay_to_state(context.compiled, state, trace)
        state = new_state
        all_trace.extend(trace)

    return all_trace
