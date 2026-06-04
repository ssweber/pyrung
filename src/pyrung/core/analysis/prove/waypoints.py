"""Waypoint planner for how().

Decomposes a target condition into intermediate waypoints (stateful tags
that must change value), orders them by dependency, and runs a scoped
mini-BFS per waypoint.  Falls back to undecomposed BFS when decomposition
fails or any mini-BFS exhausts its budget.
"""

from __future__ import annotations

import heapq
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

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


_UNFILTERED = object()


def _extract_condition_values(expr: Expr) -> dict[str, frozenset[Any]]:
    """Extract invertible tag → {possible values} from *expr*.

    For And, each term contributes independently.  For Or, a tag is
    kept only when *every* branch constrains it — values are unioned.
    Rise/fall/truthy terms contribute nothing.
    """
    if isinstance(expr, Const):
        return {}
    if isinstance(expr, Atom):
        pairs = _required_from_atom(expr)
        return {t: frozenset([v]) for t, v in pairs} if pairs else {}
    if isinstance(expr, And):
        result: dict[str, frozenset[Any]] = {}
        for term in expr.terms:
            result.update(_extract_condition_values(term))
        return result
    if isinstance(expr, Or):
        per_branch: list[dict[str, frozenset[Any]]] = []
        for term in expr.terms:
            sub = _extract_condition_values(term)
            if not sub:
                return {}
            per_branch.append(sub)
        common = set(per_branch[0].keys())
        for b in per_branch[1:]:
            common &= b.keys()
        if not common:
            return {}
        result2: dict[str, frozenset[Any]] = {}
        for tag in common:
            vals: frozenset[Any] = frozenset()
            for b in per_branch:
                vals = vals | b[tag]
            result2[tag] = vals
        return result2
    return {}


def _resolve_rung(program: Any, node: Any) -> Any | None:
    """Get the rung object for a PDG node."""
    if node.subroutine is not None:
        subs = getattr(program, "subroutines", {})
        rungs = subs.get(node.subroutine, [])
    else:
        rungs = program.rungs if hasattr(program, "rungs") else program
    if node.rung_index >= len(rungs):
        return None
    rung = rungs[node.rung_index]
    for bi in node.branch_path:
        rung = rung._branches[bi]
    return rung


def _written_value_for_tag(rung_obj: Any, tag_name: str) -> tuple[str, Any] | None:
    """Determine what a rung writes to *tag_name*.

    Returns ``("literal", value)``, ``("tag", source_name)``, or ``None``.
    """
    from pyrung.core.instruction.coils import LatchInstruction, ResetInstruction
    from pyrung.core.instruction.data_transfer import CopyInstruction, FillInstruction

    for instr in rung_obj._instructions:
        if isinstance(instr, CopyInstruction):
            dest = instr.dest
            if getattr(dest, "name", None) != tag_name:
                continue
            src = instr.source
            if hasattr(src, "name"):
                if getattr(src, "readonly", False):
                    return ("literal", src.default)
                return ("tag", src.name)
            return ("literal", src)

        if isinstance(instr, FillInstruction):
            dest = instr.dest
            dest_names = set()
            if hasattr(dest, "tags"):
                dest_names = {getattr(t, "name", None) for t in dest.tags()}
            if tag_name in dest_names:
                val = instr.value
                if hasattr(val, "name"):
                    return ("tag", val.name)
                return ("literal", val)

        if isinstance(instr, LatchInstruction):
            if getattr(instr.target, "name", None) == tag_name:
                return ("literal", True)

        if isinstance(instr, ResetInstruction):
            if getattr(instr.target, "name", None) == tag_name:
                return ("literal", False)

    return None


def _has_literal_writer(
    tag_name: str,
    value: Any,
    pdg: ProgramGraph,
    program: Any,
) -> bool:
    """True when at least one writer of *tag_name* literally produces *value*."""
    for ri in pdg.writers_of.get(tag_name, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is not None:
            wv = _written_value_for_tag(ro, tag_name)
            if wv is not None and wv[0] == "literal" and wv[1] == value:
                return True
    return False


def _value_aware_cone(
    tag_name: str,
    required_value: Any,
    pdg: ProgramGraph,
    program: Any,
    stop_at: frozenset[str] = frozenset(),
) -> frozenset[str]:
    """Upstream cone filtered to writers that can produce *required_value*.

    Tags in *stop_at* are added to the cone but their writers are not
    followed — they are assumed to be solved by a separate waypoint.
    """
    from pyrung.core.analysis.simplified import _sp_to_expr

    visited: set[tuple[str, Any]] = set()
    cone_tags: set[str] = set()
    queue: deque[tuple[str, Any]] = deque([(tag_name, required_value)])

    while queue:
        tag, val = queue.popleft()
        key = (tag, id(val) if val is _UNFILTERED else val)
        if key in visited:
            continue
        visited.add(key)
        cone_tags.add(tag)

        if tag in stop_at and tag != tag_name:
            continue

        writers = pdg.writers_of.get(tag, frozenset())

        has_literal = False
        if val is not _UNFILTERED:
            for ri in writers:
                ro = _resolve_rung(program, pdg.rung_nodes[ri])
                if ro is not None:
                    wv = _written_value_for_tag(ro, tag)
                    if wv is not None and wv[0] == "literal" and wv[1] == val:
                        has_literal = True
                        break

        for rung_idx in writers:
            node = pdg.rung_nodes[rung_idx]
            accounted: set[str] = set()
            rung_obj = _resolve_rung(program, node)

            if val is not _UNFILTERED:
                if rung_obj is not None:
                    wv = _written_value_for_tag(rung_obj, tag)
                    if wv is not None:
                        kind, wval = wv
                        if kind == "literal" and wval != val:
                            continue
                        if kind == "tag":
                            if has_literal:
                                continue
                            queue.append((wval, val))
                            accounted.add(wval)

            cond_values: dict[str, frozenset[Any]] = {}
            if rung_obj is not None:
                sp = rung_obj.sp_tree()
                if sp is not None:
                    cond_values = _extract_condition_values(_sp_to_expr(sp))

            for rt in node.condition_reads:
                vals = cond_values.get(rt)
                if vals is not None:
                    for v in vals:
                        vk = (rt, v)
                        if vk not in visited:
                            queue.append((rt, v))
                else:
                    vk = (rt, id(_UNFILTERED))
                    if vk not in visited:
                        queue.append((rt, _UNFILTERED))
            for rt in node.data_reads:
                if rt not in cone_tags and rt not in accounted:
                    queue.append((rt, _UNFILTERED))

    cone_tags.discard(tag_name)
    return frozenset(cone_tags)


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
    logger.info("how: %d unsatisfied target(s): %s", len(unsatisfied),
                ", ".join(f"{t}={v}" for t, v in unsatisfied))

    from pyrung.core.analysis.causal.support import _collect_sp_leaves
    from pyrung.core.analysis.reverse_edges import build_reverse_edge_map
    from pyrung.core.analysis.simplified import _sp_to_expr

    edge_map = build_reverse_edge_map(program)

    # Phase 1: discover all waypoint (tag, value) pairs.
    wp_pairs: list[tuple[str, Any]] = []
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

        wp_pairs.append((tag_name, required_value))

        has_literal = _has_literal_writer(tag_name, required_value, pdg, program)

        if not has_literal:
            propagated = _back_propagate_with_barriers(
                edge_map,
                tag_name,
                required_value,
                pdg,
                program,
            )
            for src_tag, src_val in propagated.items():
                if src_tag in seen or snapshot.get(src_tag) == src_val:
                    continue
                if src_tag not in pdg.writers_of:
                    continue
                can_produce = False
                for ri in pdg.writers_of[src_tag]:
                    ro = _resolve_rung(program, pdg.rung_nodes[ri])
                    if ro is not None:
                        wv = _written_value_for_tag(ro, src_tag)
                        if wv is None or wv == ("literal", src_val) or wv[0] == "tag":
                            can_produce = True
                            break
                if can_produce:
                    queue.append((src_tag, src_val))

        for rung_idx in pdg.writers_of[tag_name]:
            node = pdg.rung_nodes[rung_idx]
            rung = _resolve_rung(program, node)
            if rung is None:
                continue
            wv = _written_value_for_tag(rung, tag_name)
            if wv is not None:
                kind, wval = wv
                if kind == "literal" and wval != required_value:
                    continue
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

    # Phase 2: compute cones, stopping at other waypoint tags.
    logger.info("how: discovered %d waypoint(s): %s", len(wp_pairs),
                ", ".join(f"{t}={v}" for t, v in wp_pairs))
    wp_tag_set = frozenset(tag for tag, _ in wp_pairs)
    waypoints: list[_Waypoint] = []
    for tag_name, required_value in wp_pairs:
        cone = _value_aware_cone(tag_name, required_value, pdg, program, stop_at=wp_tag_set)
        if not cone:
            cone = pdg.upstream_slice(tag_name)
        waypoints.append(_Waypoint(tag_name, required_value, cone))

    return waypoints


def _back_propagate_with_barriers(
    edge_map: dict[str, list[tuple[str, Any]]],
    tag_name: str,
    value: Any,
    pdg: ProgramGraph,
    program: Any,
) -> dict[str, Any]:
    """Like ``back_propagate_value`` but stops at literal barriers.

    When a tag along the copy chain already has a literal writer for
    the propagated value, further tracing through that tag is skipped —
    the literal write is a sufficient source and upstream copy sources
    are irrelevant.
    """
    by_target: dict[str, list[tuple[str, Any]]] = {}
    for source, edges in edge_map.items():
        for target, invert in edges:
            by_target.setdefault(target, []).append((source, invert))

    result: dict[str, Any] = {}
    queue: list[tuple[str, Any]] = [(tag_name, value)]
    seen: set[str] = {tag_name}

    while queue:
        current_tag, current_value = queue.pop()

        if current_tag != tag_name:
            if _has_literal_writer(current_tag, current_value, pdg, program):
                continue

        for source, invert in by_target.get(current_tag, []):
            if source in seen:
                continue
            inferred = invert(current_value)
            if inferred is None:
                continue
            seen.add(source)
            result[source] = inferred
            if source in by_target:
                queue.append((source, inferred))

    return result


def _order_waypoints(
    waypoints: list[_Waypoint],
    pdg: ProgramGraph,
) -> list[_Waypoint] | None:
    """Topological sort of waypoints by condition-reads dependency.

    When cyclic dependencies exist (common in state machines where
    StateCurrent ↔ StateRequested), the cycle members are merged into
    a single mega-waypoint with a combined cone so the scoped BFS can
    solve them together.  Non-cyclic waypoints that merely depend on a
    cycle are kept separate and ordered after it.
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

    # --- SCC detection (Tarjan's) to merge only true cycles ---
    sccs = _tarjan_sccs(wp_tags, deps)

    # Merge each multi-member SCC into a single waypoint, keep singletons.
    scc_wps: list[_Waypoint] = []
    tag_to_scc: dict[str, int] = {}
    for idx, scc in enumerate(sccs):
        for t in scc:
            tag_to_scc[t] = idx
        if len(scc) == 1:
            scc_wps.append(wp_by_tag[scc[0]])
        else:
            primary = min(scc, key=lambda t: len(wp_by_tag[t].cone))
            merged_cone: frozenset[str] = frozenset()
            for t in scc:
                merged_cone = merged_cone | wp_by_tag[t].cone
            extra = frozenset(t for t in scc if t != primary)
            merged_cone = merged_cone | extra
            scc_wps.append(_Waypoint(
                wp_by_tag[primary].tag_name,
                wp_by_tag[primary].required_value,
                merged_cone,
            ))
            logger.info("how: merged cycle {%s} into mega-waypoint",
                        ", ".join(sorted(scc)))

    # Build condensed dependency graph over SCCs and topo-sort.
    scc_deps: dict[int, set[int]] = {i: set() for i in range(len(sccs))}
    for tag, tag_deps in deps.items():
        src = tag_to_scc[tag]
        for dep in tag_deps:
            dst = tag_to_scc[dep]
            if dst != src:
                scc_deps[src].add(dst)

    in_degree = {i: len(d) for i, d in scc_deps.items()}
    ready: list[tuple[int, int]] = sorted(
        (len(scc_wps[i].cone), i) for i, d in in_degree.items() if d == 0
    )
    heapq.heapify(ready)
    ordered: list[_Waypoint] = []

    while ready:
        _, scc_idx = heapq.heappop(ready)
        ordered.append(scc_wps[scc_idx])
        for other, other_deps in scc_deps.items():
            if scc_idx in other_deps:
                other_deps.discard(scc_idx)
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    heapq.heappush(ready, (len(scc_wps[other].cone), other))

    if len(ordered) < len(scc_wps):
        remaining = [wp for i, wp in enumerate(scc_wps) if wp not in ordered]
        primary = remaining[0]
        fallback_cone: frozenset[str] = frozenset()
        for wp in remaining:
            fallback_cone = fallback_cone | wp.cone
        fallback_extra = frozenset(
            wp.tag_name for wp in remaining if wp.tag_name != primary.tag_name
        )
        fallback_cone = fallback_cone | fallback_extra
        ordered.append(_Waypoint(primary.tag_name, primary.required_value, fallback_cone))

    return ordered


def _tarjan_sccs(
    nodes: set[str],
    deps: dict[str, set[str]],
) -> list[list[str]]:
    """Tarjan's SCC algorithm. Returns SCCs in reverse topological order."""
    index_counter = [0]
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlinks: dict[str, int] = {}
    result: list[list[str]] = []

    def strongconnect(v: str) -> None:
        indices[v] = lowlinks[v] = index_counter[0]
        index_counter[0] += 1
        stack.append(v)
        on_stack.add(v)

        for w in deps.get(v, ()):
            if w not in nodes:
                continue
            if w not in indices:
                strongconnect(w)
                lowlinks[v] = min(lowlinks[v], lowlinks[w])
            elif w in on_stack:
                lowlinks[v] = min(lowlinks[v], indices[w])

        if lowlinks[v] == indices[v]:
            scc: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                scc.append(w)
                if w == v:
                    break
            result.append(scc)

    for v in sorted(nodes):
        if v not in indices:
            strongconnect(v)

    return result


def _run_waypoint_plan(
    waypoints: list[_Waypoint],
    snapshot: dict[str, Any],
    target_pred: Any,
    program: Any,
    max_steps: int,
    opt: Any,
    compiled: Any = None,
    state_filter: Any = None,
    pipeline_cache: Any = None,
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
    logger.info("how: running %d waypoint(s), budget=%d per waypoint",
                len(waypoints), budget_per_wp)
    current_state = dict(snapshot)
    all_trace_steps: list[Any] = []

    wp_generators: list[Any] = []

    for i, wp in enumerate(waypoints):
        logger.info("how: waypoint %d/%d: %s=%s (cone=%d tags)",
                     i + 1, len(waypoints), wp.tag_name, wp.required_value, len(wp.cone))
        wp_pred = _make_wp_predicate(wp)
        scope = sorted(wp.cone | {wp.tag_name})

        # Observe the waypoint tag so exclusive-input-group detection engages
        # (it no-ops unless project/extra_exprs is set — see inputs.py).  Without
        # it, mutually-exclusive command inputs that share an encoder (e.g. the
        # PackML CtrlCmd family) are enumerated as a full 2^N cross-product per
        # state instead of N+1 canonical assignments — a >100x BFS blowup.  The
        # waypoint tag is always stateful (never an input), so projecting it
        # cannot widen the state key.
        context = _build_explore_context(
            program,
            scope=scope,
            project=(wp.tag_name,),
            _opt_config=opt,
            compiled=compiled,
            initial_state=current_state,
            pipeline_cache=pipeline_cache,
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
            state_filter=state_filter,
        )

        result_list = next(gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            logger.info("how: waypoint %d/%d exhausted, backtracking",
                         i + 1, len(waypoints))
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
                state_filter=state_filter,
                pipeline_cache=pipeline_cache,
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
        for n, v in step.prev.items():
            kernel.prev[n] = v
        for n, v in step.inputs.items():
            kernel.tags[n] = v
        for _ in range(step.scans):
            _step_compiled_kernel(compiled, kernel, dt=0.010)
    return dict(kernel.tags)


def _find_backjump_target(
    conflict_tags: frozenset[str],
    waypoints: list[Any],
    wp_generators: list[Any],
) -> int | None:
    """Find the latest prior waypoint whose tag is in *conflict_tags*."""
    for j in range(len(wp_generators) - 1, -1, -1):
        if waypoints[j].tag_name in conflict_tags:
            return j
    return None


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
    max_retries: int = 5,
    compiled: Any = None,
    state_filter: Any = None,
    pipeline_cache: Any = None,
) -> list[Any] | None:
    """Try resuming a previous waypoint's generator for an alternate path.

    Uses dependency-directed backjumping: skips generators whose waypoint
    tag is not in the failed waypoint's upstream cone.

    Returns the combined trace on success, or ``None`` on exhaustion.
    """
    from pyrung.core.analysis.prove.results import Counterexample

    conflict_tags = waypoints[failed_idx].cone
    target = _find_backjump_target(conflict_tags, waypoints, wp_generators)
    if target is not None:
        del wp_generators[target + 1 :]

    for _attempt in range(max_retries):
        if not wp_generators:
            return None

        prev_gen, prev_context, trace_start = wp_generators[-1]
        prev_wp_idx = len(wp_generators) - 1
        wp_generators.pop()

        result_list = next(prev_gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            # Merge exhausted waypoint's cone so the next jump stays directed
            conflict_tags = conflict_tags | waypoints[prev_wp_idx].cone
            target = _find_backjump_target(conflict_tags, waypoints, wp_generators)
            if target is not None:
                del wp_generators[target + 1 :]
            continue

        prev_trace = result_list[0].trace

        steps_before = all_trace_steps[:trace_start]

        if trace_start > 0:
            state_before = _replay_to_state(prev_context.compiled, original_snapshot, steps_before)
        else:
            state_before = dict(original_snapshot)

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
            state_filter=state_filter,
            pipeline_cache=pipeline_cache,
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
    state_filter: Any = None,
    pipeline_cache: Any = None,
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
        # Observe the waypoint tag so exclusive-input-group detection engages —
        # see the note in _run_waypoint_plan.
        context = _build_explore_context(
            program,
            scope=scope,
            project=(wp.tag_name,),
            _opt_config=opt,
            compiled=compiled,
            initial_state=state,
            pipeline_cache=pipeline_cache,
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
            state_filter=state_filter,
        )

        result_list = next(gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            return None

        trace = result_list[0].trace
        new_state = _replay_to_state(context.compiled, state, trace)
        state = new_state
        all_trace.extend(trace)

    return all_trace
