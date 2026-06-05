"""Waypoint planner for how().

Decomposes a target condition into intermediate waypoints (stateful tags
that must change value), orders them by dependency, and runs a scoped
mini-BFS per waypoint.  Falls back to undecomposed BFS when decomposition
fails or any mini-BFS exhausts its budget.

Discovery uses landmark extraction (Hoffmann et al. 2004): build a relaxed
planning graph from the PDG, backchain from the goal intersecting achiever
preconditions, and keep only facts required by ALL first achievers.
"""

from __future__ import annotations

import heapq
import logging
from collections import deque
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# Per-search kernel-eval budget for how() (consumed by bfs.py's _eval_count guard).
# Bounds each scoped/undecomposed BFS so a hard target fails fast as Intractable
# instead of hanging.  Deterministic (eval count, not wall-clock) to keep how()
# results reproducible across machines.  Tunable: raise if a legitimately solvable
# target needs a deeper search; the L2 mega-waypoint gate keeps the common hard
# case from ever reaching this ceiling.
_HOW_EVAL_BUDGET = 500_000

# Cone-size ceiling above which a (typically SCC-merged) waypoint is not worth a
# scoped BFS: its cone is so close to the whole program that the undecomposed
# fallback is no harder and skips the per-waypoint context rebuild.  Gating here
# turns "decompose, burn the eval budget, then fall back" into an immediate
# fallback — fast instead of merely bounded.  L1's eval budget is the safety net;
# this is the speed optimisation layered on top.
_MEGA_CONE_LIMIT = 18

from pyrung.core.analysis.pdg import ProgramGraph, TagRole
from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or

Fact = tuple[str, Any]


@dataclass(frozen=True)
class _Action:
    rung_index: int
    preconditions: frozenset[Fact]
    add_effects: frozenset[Fact]


@dataclass(frozen=True)
class _Waypoint:
    tag_name: str
    required_value: Any
    cone: frozenset[str]
    # True when this cone was widened by kernel probing (_probe_cone_expansion).
    # Such cones are intentionally larger than the static cone, so they are exempt
    # from the L2 mega-cone fail-fast gate in _run_waypoint_plan.
    probe_expanded: bool = False
    # True for a per-value-step sub-waypoint emitted by _try_decompose_scc (e.g.
    # Step=1, Step=2, ... for a counter).  Its cost is governed by search *depth*
    # — one value step is a few scans — not by cone *width*, so it is likewise
    # exempt from the L2 cone-size gate even when the cone spans the whole cycle.
    value_stepped: bool = False


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

    Returns ``("literal", value)``, ``("tag", source_name)``,
    ``("increment", step)`` for ``calc(tag + N, tag)``,
    ``("decrement", step)`` for ``calc(tag - N, tag)``,
    or ``None``.
    """
    from pyrung.core.instruction.calc import CalcInstruction
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

        if isinstance(instr, CalcInstruction):
            if getattr(instr.dest, "name", None) != tag_name:
                continue
            result = _detect_arithmetic_pattern(instr.expression, tag_name)
            if result is not None:
                return result
            return None

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


def _detect_arithmetic_pattern(
    expression: Any,
    tag_name: str,
) -> tuple[str, Any] | None:
    """Detect ``tag + N`` or ``tag - N`` patterns in a calc expression."""
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr

    if not isinstance(expression, BinaryExpr):
        return None

    op_symbol = expression.symbol

    if op_symbol == "+":
        if (
            isinstance(expression.left, TagExpr)
            and getattr(expression.left.tag, "name", None) == tag_name
            and isinstance(expression.right, LiteralExpr)
        ):
            return ("increment", expression.right.value)
        if (
            isinstance(expression.right, TagExpr)
            and getattr(expression.right.tag, "name", None) == tag_name
            and isinstance(expression.left, LiteralExpr)
        ):
            return ("increment", expression.left.value)

    if op_symbol == "-":
        if (
            isinstance(expression.left, TagExpr)
            and getattr(expression.left.tag, "name", None) == tag_name
            and isinstance(expression.right, LiteralExpr)
        ):
            return ("decrement", expression.right.value)

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


def _has_arithmetic_writer(tag_name: str, pdg: ProgramGraph, program: Any) -> bool:
    """True when some writer increments/decrements *tag_name* (a ±1 counter).

    Marks tags whose value path can be walked one step at a time through their
    domain — the precondition for domain-stepping decomposition.
    """
    for ri in pdg.writers_of.get(tag_name, frozenset()):
        ro = _resolve_rung(program, pdg.rung_nodes[ri])
        if ro is not None:
            wv = _written_value_for_tag(ro, tag_name)
            if wv is not None and wv[0] in ("increment", "decrement"):
                return True
    return False


def _advance_range_contains(value: Any, from_value: Any, target_value: Any) -> bool:
    """True if *value* lies on the path from *from_value* toward *target_value*.

    Half-open: includes ``from_value``, excludes ``target_value`` — machinery
    that only fires *at* the target value isn't needed to *reach* it.  Non-numeric
    values are kept (never pruned) to stay conservative.
    """
    if not isinstance(value, (int, float)):
        return True
    if from_value == target_value:
        return value == from_value
    if from_value < target_value:
        return from_value <= value < target_value
    return target_value < value <= from_value


def _value_aware_cone(
    tag_name: str,
    required_value: Any,
    pdg: ProgramGraph,
    program: Any,
    stop_at: frozenset[str] = frozenset(),
    from_value: Any = None,
) -> frozenset[str]:
    """Upstream cone filtered to writers that can produce *required_value*.

    Tags in *stop_at* are added to the cone but their writers are not
    followed — they are assumed to be solved by a separate waypoint.

    When *from_value* is given (a per-step sub-waypoint of a counter advancing
    ``from_value → required_value``), intermediate-machinery writers that can
    only fire while the stepper sits at a value *outside* the ``[from, target)``
    window are pruned — e.g. advancing Step 1→2 must not pull in the level/timer
    inputs of a sub-state that is only active at Step==3.  This keeps each
    per-step cone (and its free-input branching factor) small.
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

            # From-value pruning (per-step counter cones): skip intermediate
            # machinery that can only fire while the stepper sits outside the
            # advance window.  Never prune the stepper's own writers (tag ==
            # tag_name) — their enabler chase is intentionally partial (it can't
            # invert e.g. modulo gates), so a partial result must not gate them.
            if from_value is not None and tag != tag_name:
                enabling = _constraining_from_values(
                    tag_name, node, program, pdg, _MAX_ENABLER_CHASE, {rung_idx}
                )
                if enabling and not any(
                    _advance_range_contains(v, from_value, required_value) for v in enabling
                ):
                    continue

            # Per-step counter cone: drop the stepper's own arithmetic writers
            # that move *away* from the target (the decrement/wrap rungs when
            # advancing up, or vice versa).  They cannot contribute to this step,
            # and their guards drag in free inputs — each free input roughly
            # triples the per-scan BFS branching — e.g. a ``Step==5, HMI_reset``
            # decrement otherwise injects HMI_reset into a Step 1→3 cone.
            if (
                from_value is not None
                and tag == tag_name
                and rung_obj is not None
                and isinstance(from_value, (int, float))
                and isinstance(required_value, (int, float))
            ):
                wv_dir = _written_value_for_tag(rung_obj, tag)
                if wv_dir is not None and wv_dir[0] in ("increment", "decrement"):
                    going_up = required_value > from_value
                    if (going_up and wv_dir[0] == "decrement") or (
                        not going_up and wv_dir[0] == "increment"
                    ):
                        continue

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


def _probe_cone_expansion(
    cone_tags: frozenset[str],
    target_tag: str,
    compiled: Any,
    current_state: dict[str, Any],
    pipeline_cache: Any | None,
    dt: float = 0.010,
    num_scans: int = 3,
    max_iterations: int = 2,
) -> frozenset[str]:
    """Expand a static cone by probing the kernel for hidden dependencies.

    When the PDG-derived cone is too narrow (e.g. indirect array access that
    the PDG cannot trace), this function runs the compiled kernel with varied
    inputs to discover which external tags actually affect cone tags at runtime.
    """
    from pyrung.core.analysis.prove.kernel import (
        _restore_kernel,
        _snapshot_kernel,
        _step_compiled_kernel,
    )

    observe = set(cone_tags) | {target_tag}
    total_tags = len(compiled.referenced_tags)
    discovered: set[str] = set()

    nd_dims = pipeline_cache.nondeterministic_dims if pipeline_cache else {}
    sd_dims = pipeline_cache.stateful_dims if pipeline_cache else {}

    from pyrung.core.tag import TagType

    _INT_TYPES = {TagType.INT, TagType.DINT, TagType.WORD}

    for _iteration in range(max_iterations):
        candidates: list[tuple[str, list[Any]]] = []
        for name, tag in compiled.referenced_tags.items():
            if name in observe:
                continue
            if name in nd_dims:
                candidates.append((name, list(nd_dims[name])))
            elif name in sd_dims:
                candidates.append((name, list(sd_dims[name])))
            elif tag.type is TagType.BOOL:
                candidates.append((name, [True, False]))
            elif tag.type in _INT_TYPES:
                d = current_state.get(name, tag.default)
                candidates.append((name, [d - 1, d + 1]))
            elif tag.type is TagType.REAL:
                d = current_state.get(name, tag.default)
                candidates.append((name, [d - 1.0, d + 1.0]))

        if not candidates:
            break

        kernel = compiled.create_kernel()
        for n, v in current_state.items():
            if n in kernel.tags:
                kernel.tags[n] = v
        snap = _snapshot_kernel(kernel)

        # Baseline over ALL referenced tags, not just the observe set.  An affecting
        # candidate may reach the cone through an intermediary that is itself
        # rewritten every scan (e.g. B in an A->B->cone chain): B is invisible to
        # initial-value perturbation, but it *does* differ from baseline when the
        # candidate changes, so the per-scan diff below surfaces it as a cone tag.
        all_tags = tuple(compiled.referenced_tags)
        _restore_kernel(kernel, snap)
        baseline: list[dict[str, Any]] = []
        for _ in range(num_scans):
            _step_compiled_kernel(compiled, kernel, dt=dt)
            baseline.append({t: kernel.tags.get(t) for t in all_tags})

        round_found: set[str] = set()
        for cand_name, domain in candidates:
            cone_affecting = False
            changed: set[str] = set()
            for val in domain:
                _restore_kernel(kernel, snap)
                kernel.tags[cand_name] = val
                for scan_idx in range(num_scans):
                    _step_compiled_kernel(compiled, kernel, dt=dt)
                    for t in all_tags:
                        if kernel.tags.get(t) != baseline[scan_idx].get(t):
                            changed.add(t)
                            if t in observe:
                                cone_affecting = True
                if cone_affecting:
                    break
            if cone_affecting:
                # Add the candidate and every tag it perturbs en route to the cone.
                round_found.add(cand_name)
                round_found |= changed

        if not round_found:
            break

        discovered |= round_found
        observe |= round_found

        if len(observe) > total_tags * 3 // 4:
            logger.warning(
                "how: cone expansion exceeded 75%% of program tags (%d/%d), capping",
                len(observe),
                total_tags,
            )
            break

    if not discovered:
        return cone_tags
    return cone_tags | frozenset(discovered)


def _get_domain(tag_name: str, pipeline_cache: Any) -> tuple[Any, ...] | None:
    """Look up a tag's seeded domain from the pipeline cache."""
    if pipeline_cache is None:
        return None
    domain = getattr(pipeline_cache, "stateful_dims", {}).get(tag_name)
    if domain is None:
        domain = getattr(pipeline_cache, "nondeterministic_dims", {}).get(tag_name)
    return domain


def _frontier_has_progress(
    frontier_states: list[dict[str, Any]],
    tag_name: str,
    initial_value: Any,
) -> bool:
    """True when any frontier state shows the tag advanced beyond its initial value."""
    return any(s.get(tag_name) != initial_value for s in frontier_states if tag_name in s)


def _discover_waypoints(
    snapshot: dict[str, Any],
    target_expr: Expr,
    pdg: ProgramGraph,
    program: Any,
) -> tuple[list[_Waypoint], dict[Fact, set[Fact]], list[_Action], dict[Fact, set[int]]] | None:
    """Find stateful tags that must change to satisfy *target_expr*.

    Two-pass approach:
    1. Run the upstream walk to discover all candidate waypoints.
    2. Build an RPG and extract landmarks.  If landmarks are found, filter
       the candidates to only those that appear as landmarks — this prunes
       non-essential waypoints.  If the RPG is too weak (e.g. tag copies
       it can't model), keep all candidates.

    Returns ``(waypoints, orderings, actions, first_achievers)`` on success,
    or ``None`` when the expression cannot be decomposed.
    """
    required = _extract_required_values(target_expr, snapshot)
    if required is None:
        return None

    unsatisfied = [(tag, val) for tag, val in required if snapshot.get(tag) != val]
    if not unsatisfied:
        return [], {}, [], {}
    logger.info(
        "how: %d unsatisfied target(s): %s",
        len(unsatisfied),
        ", ".join(f"{t}={v}" for t, v in unsatisfied),
    )

    # Pass 1: upstream walk discovers all candidate waypoints.
    all_candidates = _discover_waypoints_fallback(snapshot, unsatisfied, pdg, program)

    # Pass 2: landmark extraction to filter candidates.
    actions = _build_actions(pdg, program)
    initial_facts: frozenset[Fact] = frozenset((t, v) for t, v in snapshot.items())
    goal_facts: frozenset[Fact] = frozenset(unsatisfied)

    first_achievers, goal_reachable = _build_rpg(actions, initial_facts, goal_facts)

    orderings: dict[Fact, set[Fact]] = {}

    if goal_reachable:
        landmarks, orderings = _extract_landmarks(
            actions,
            first_achievers,
            goal_facts,
            initial_facts,
        )

        landmark_tags = {tag for tag, _val in landmarks}

        # Filter candidates: keep only those that are landmarks (i.e. required
        # by ALL achievers).  If landmarks found intermediate waypoints beyond
        # the goal, apply the filter; otherwise keep all candidates.
        has_intermediate = any(tag not in {t for t, _ in unsatisfied} for tag in landmark_tags)
        if has_intermediate:
            filtered = [(t, v) for t, v in all_candidates if (t, v) in landmarks]
            if filtered:
                logger.info(
                    "how: landmark filter reduced %d → %d waypoint(s)",
                    len(all_candidates),
                    len(filtered),
                )
                all_candidates = filtered
    else:
        logger.info("how: RPG cannot reach goal (tag copies or complex writes)")

    wp_pairs = all_candidates

    logger.info(
        "how: discovered %d waypoint(s): %s",
        len(wp_pairs),
        ", ".join(f"{t}={v}" for t, v in wp_pairs),
    )

    wp_tag_set = frozenset(tag for tag, _ in wp_pairs)
    waypoints: list[_Waypoint] = []
    for tag_name, required_value in wp_pairs:
        cone = _value_aware_cone(
            tag_name,
            required_value,
            pdg,
            program,
            stop_at=wp_tag_set,
        )
        if not cone:
            cone = pdg.upstream_slice(tag_name)
        waypoints.append(_Waypoint(tag_name, required_value, cone))

    return waypoints, orderings, actions, first_achievers


def _discover_waypoints_fallback(
    snapshot: dict[str, Any],
    unsatisfied: list[Fact],
    pdg: ProgramGraph,
    program: Any,
) -> list[Fact]:
    """Original upstream-walk discovery as fallback.

    Used when landmark extraction finds no intermediate waypoints (e.g.
    non-literal writes, copy chains without invertible conditions).
    """
    from pyrung.core.analysis.causal.support import _collect_sp_leaves
    from pyrung.core.analysis.reverse_edges import build_reverse_edge_map
    from pyrung.core.analysis.simplified import _sp_to_expr

    edge_map = build_reverse_edge_map(program)

    wp_pairs: list[Fact] = []
    seen: set[str] = set()
    queue: deque[Fact] = deque(unsatisfied)

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

    return wp_pairs


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


# ---------------------------------------------------------------------------
# RPG / landmark extraction (Hoffmann et al. 2004)
# ---------------------------------------------------------------------------


def _build_actions(
    pdg: ProgramGraph,
    program: Any,
) -> list[_Action]:
    """Build planning-style actions from the PDG.

    One action per (rung, written_tag, literal_value) triple.  Preconditions
    come from ``_extract_condition_values`` on the rung's condition tree.
    INPUT tags are excluded from preconditions since the operator can
    freely set them.
    """
    from pyrung.core.analysis.simplified import _sp_to_expr

    input_tags = {t for t, r in pdg.tag_roles.items() if r == TagRole.INPUT}
    actions: list[_Action] = []
    for ri, node in enumerate(pdg.rung_nodes):
        rung = _resolve_rung(program, node)
        if rung is None:
            continue

        sp = rung.sp_tree()
        if sp is not None:
            cond_values = _extract_condition_values(_sp_to_expr(sp))
        else:
            cond_values = {}

        precond_facts: frozenset[Fact] = frozenset(
            (t, v) for t, vals in cond_values.items() for v in vals if t not in input_tags
        )

        for wt in node.writes:
            wv = _written_value_for_tag(rung, wt)
            if wv is None:
                continue
            kind, wval = wv
            if kind == "literal":
                actions.append(_Action(ri, precond_facts, frozenset({(wt, wval)})))
            elif kind == "increment":
                actions.append(_Action(ri, precond_facts, frozenset({(wt, ("__inc__", wval))})))
            elif kind == "decrement":
                actions.append(_Action(ri, precond_facts, frozenset({(wt, ("__dec__", wval))})))

    return actions


def _build_rpg(
    actions: list[_Action],
    initial_facts: frozenset[Fact],
    goal_facts: frozenset[Fact],
) -> tuple[dict[Fact, set[int]], bool]:
    """Build a relaxed planning graph (delete-relaxation).

    Returns ``(first_achievers, goal_reachable)`` where *first_achievers*
    maps each newly-discovered fact to the set of action indices that first
    produced it.
    """
    current = set(initial_facts)
    first_achievers: dict[Fact, set[int]] = {}

    for _ in range(len(actions) + 1):
        if goal_facts <= current:
            return first_achievers, True

        new_facts: set[Fact] = set()
        for ai, action in enumerate(actions):
            if action.preconditions <= current:
                for fact in action.add_effects:
                    if fact not in current:
                        new_facts.add(fact)
                        first_achievers.setdefault(fact, set()).add(ai)

        if not new_facts:
            break
        current |= new_facts

    return first_achievers, goal_facts <= current


def _extract_landmarks(
    actions: list[_Action],
    first_achievers: dict[Fact, set[int]],
    goal_facts: frozenset[Fact],
    initial_facts: frozenset[Fact],
) -> tuple[set[Fact], dict[Fact, set[Fact]]]:
    """Extract landmarks via RPG backchaining with all-achievers intersection.

    A fact ``q`` is a landmark if it appears in the preconditions of **every**
    first achiever of some downstream landmark ``p``.  Returns the landmark
    set and greedy-necessary orderings ``{p: {predecessors}}``.
    """
    landmarks: set[Fact] = set()
    orderings: dict[Fact, set[Fact]] = {}
    queue: deque[Fact] = deque()

    for g in goal_facts:
        landmarks.add(g)
        queue.append(g)

    while queue:
        p = queue.popleft()
        achievers = first_achievers.get(p)
        if not achievers:
            continue

        shared: set[Fact] | None = None
        for ai in achievers:
            preconds = set(actions[ai].preconditions)
            if shared is None:
                shared = preconds
            else:
                shared &= preconds

        if not shared:
            continue

        for q in shared:
            if q in initial_facts:
                continue
            orderings.setdefault(p, set()).add(q)
            if q not in landmarks:
                landmarks.add(q)
                queue.append(q)

    return landmarks, orderings


def _compute_reasonable_orderings(
    landmarks: set[Fact],
    actions: list[_Action],
    first_achievers: dict[Fact, set[int]],
) -> dict[Fact, set[Fact]]:
    """Compute reasonable orderings: ``p →_reas q``.

    ``p`` must be achieved before ``q`` if every first achiever of ``q``
    requires ``p``'s tag to have a *different* value than ``p`` specifies.
    """
    reasonable: dict[Fact, set[Fact]] = {}
    for q in landmarks:
        q_achievers = first_achievers.get(q)
        if not q_achievers:
            continue
        for p in landmarks:
            if p == q or p[0] == q[0]:
                continue
            all_conflict = True
            for ai in q_achievers:
                preconds_for_tag = {v for t, v in actions[ai].preconditions if t == p[0]}
                if not preconds_for_tag or p[1] in preconds_for_tag:
                    all_conflict = False
                    break
            if all_conflict:
                reasonable.setdefault(q, set()).add(p)

    return reasonable


# ---------------------------------------------------------------------------
# Frontier-based refinement (Phase 3)
# ---------------------------------------------------------------------------

_MAX_REFINEMENTS = 3


@dataclass(frozen=True)
class _RefinementResult:
    strategy: str
    waypoints: list[_Waypoint]


def _domain_value_path(
    initial_value: Any,
    target_value: Any,
    domain: tuple[Any, ...],
) -> list[Any] | None:
    """Build an intermediate value path from a seeded domain.

    Filters domain values that fall strictly between *initial_value* and
    *target_value*, sorts toward the target, and appends the target.
    Returns ``None`` when there are no intermediates.
    """
    if not isinstance(initial_value, (int, float)) or not isinstance(target_value, (int, float)):
        return None

    ascending = initial_value < target_value
    lo = min(initial_value, target_value)
    hi = max(initial_value, target_value)
    intermediates = sorted(
        (v for v in domain if isinstance(v, (int, float)) and lo < v < hi),
        reverse=not ascending,
    )
    if not intermediates:
        return None

    return intermediates + [target_value]


def _analyze_frontier_value_gap(
    frontier_states: list[dict[str, Any]],
    wp: _Waypoint,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    existing_wp_tags: frozenset[str],
    pipeline_cache: Any = None,
) -> _RefinementResult | None:
    """Strategy (a): find intermediate values of the target tag in frontier.

    If the target tag progressed partway (e.g. Step reaches 2 but needs 3),
    the intermediate values become sub-waypoints.  First tries the static
    value-transition graph; falls back to the seeded domain from the
    pipeline cache when the static analysis can't parse the pattern.
    """
    initial_value = snapshot.get(wp.tag_name)
    frontier_values = {state.get(wp.tag_name) for state in frontier_states if wp.tag_name in state}
    frontier_values.discard(initial_value)
    frontier_values.discard(wp.required_value)

    if not frontier_values:
        return None

    # Try static transition graph first.
    path: list[Any] | None = None
    transitions = _build_value_transitions(wp.tag_name, pdg, program)
    if transitions:
        full_path = _shortest_value_path(initial_value, wp.required_value, transitions)
        if full_path is not None and len(full_path) > 2:
            has_frontier_intermediate = any(v in frontier_values for v in full_path[1:-1])
            if has_frontier_intermediate:
                path = full_path[1:]

    # Fallback: use seeded domain from pipeline cache.
    if path is None and pipeline_cache is not None:
        domain = getattr(pipeline_cache, "stateful_dims", {}).get(wp.tag_name)
        if domain is None:
            domain = getattr(pipeline_cache, "nondeterministic_dims", {}).get(wp.tag_name)
        if domain is not None:
            path = _domain_value_path(initial_value, wp.required_value, domain)

    if path is None:
        return None

    stop_at = existing_wp_tags - {wp.tag_name}
    sub_waypoints: list[_Waypoint] = []
    for intermediate_value in path:
        cone = _value_aware_cone(
            wp.tag_name,
            intermediate_value,
            pdg,
            program,
            stop_at=stop_at,
        )
        if not cone:
            cone = pdg.upstream_slice(wp.tag_name)
        sub_waypoints.append(_Waypoint(wp.tag_name, intermediate_value, cone))

    logger.info(
        "how: frontier value-gap refined %s into %d step(s): %s",
        wp.tag_name,
        len(sub_waypoints),
        " → ".join(str(w.required_value) for w in sub_waypoints),
    )
    return _RefinementResult("value_gap", sub_waypoints)


def _analyze_frontier_condition_blocking(
    frontier_states: list[dict[str, Any]],
    wp: _Waypoint,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    existing_wp_tags: frozenset[str],
) -> _RefinementResult | None:
    """Strategy (b): find condition tags that block the writer rungs.

    For each writer rung of wp.tag_name that can produce wp.required_value,
    extract its conditions.  If a condition (tag, value) is never satisfied
    across all frontier states, it's a blocking predicate.
    """
    from pyrung.core.analysis.simplified import _sp_to_expr

    blocking: list[Fact] = []
    input_tags = {t for t, r in pdg.tag_roles.items() if r == TagRole.INPUT}

    for rung_idx in pdg.writers_of.get(wp.tag_name, frozenset()):
        node = pdg.rung_nodes[rung_idx]
        rung = _resolve_rung(program, node)
        if rung is None:
            continue

        wv = _written_value_for_tag(rung, wp.tag_name)
        if wv is None:
            continue
        kind, wval = wv
        if kind == "literal" and wval != wp.required_value:
            continue

        sp = rung.sp_tree()
        if sp is None:
            continue
        cond_values = _extract_condition_values(_sp_to_expr(sp))

        for cond_tag, needed_vals in cond_values.items():
            if cond_tag in input_tags:
                continue
            if cond_tag not in pdg.writers_of:
                continue
            for needed_val in needed_vals:
                if snapshot.get(cond_tag) == needed_val:
                    continue
                if cond_tag in existing_wp_tags:
                    continue
                satisfied = any(state.get(cond_tag) == needed_val for state in frontier_states)
                if not satisfied:
                    blocking.append((cond_tag, needed_val))

    seen: set[str] = set()
    unique_blocking: list[Fact] = []
    for tag, val in blocking:
        if tag not in seen:
            seen.add(tag)
            unique_blocking.append((tag, val))

    if not unique_blocking:
        return None

    stop_at = existing_wp_tags | {wp.tag_name}
    sub_waypoints: list[_Waypoint] = []
    for tag, val in unique_blocking:
        cone = _value_aware_cone(tag, val, pdg, program, stop_at=stop_at)
        if not cone:
            cone = pdg.upstream_slice(tag)
        sub_waypoints.append(_Waypoint(tag, val, cone))

    sub_waypoints.append(wp)

    logger.info(
        "how: frontier condition-blocking found %d prerequisite(s) for %s: %s",
        len(unique_blocking),
        wp.tag_name,
        ", ".join(f"{t}={v}" for t, v in unique_blocking),
    )
    return _RefinementResult("condition_blocking", sub_waypoints)


def _analyze_frontier_dependency_chain(
    frontier_states: list[dict[str, Any]],
    wp: _Waypoint,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    existing_wp_tags: frozenset[str],
) -> _RefinementResult | None:
    """Strategy (c): trace backward from blocking conditions through copy chains."""
    from pyrung.core.analysis.reverse_edges import build_reverse_edge_map
    from pyrung.core.analysis.simplified import _sp_to_expr

    input_tags = {t for t, r in pdg.tag_roles.items() if r == TagRole.INPUT}
    edge_map = build_reverse_edge_map(program)

    blocking_roots: list[Fact] = []

    for rung_idx in pdg.writers_of.get(wp.tag_name, frozenset()):
        node = pdg.rung_nodes[rung_idx]
        rung = _resolve_rung(program, node)
        if rung is None:
            continue

        wv = _written_value_for_tag(rung, wp.tag_name)
        if wv is None:
            continue
        kind, wval = wv
        if kind == "literal" and wval != wp.required_value:
            continue

        sp = rung.sp_tree()
        if sp is None:
            continue
        cond_values = _extract_condition_values(_sp_to_expr(sp))

        for cond_tag, needed_vals in cond_values.items():
            if cond_tag in input_tags:
                continue
            for needed_val in needed_vals:
                if snapshot.get(cond_tag) == needed_val:
                    continue
                satisfied = any(state.get(cond_tag) == needed_val for state in frontier_states)
                if not satisfied:
                    propagated = _back_propagate_with_barriers(
                        edge_map,
                        cond_tag,
                        needed_val,
                        pdg,
                        program,
                    )
                    for src_tag, src_val in propagated.items():
                        if src_tag in input_tags:
                            continue
                        if src_tag in existing_wp_tags:
                            continue
                        if snapshot.get(src_tag) == src_val:
                            continue
                        if src_tag in pdg.writers_of:
                            blocking_roots.append((src_tag, src_val))

                    if cond_tag not in existing_wp_tags and cond_tag in pdg.writers_of:
                        blocking_roots.append((cond_tag, needed_val))

    seen: set[str] = set()
    unique_roots: list[Fact] = []
    for tag, val in blocking_roots:
        if tag not in seen:
            seen.add(tag)
            unique_roots.append((tag, val))

    if not unique_roots:
        return None

    stop_at = existing_wp_tags | {wp.tag_name}
    sub_waypoints: list[_Waypoint] = []
    for tag, val in unique_roots:
        cone = _value_aware_cone(tag, val, pdg, program, stop_at=stop_at)
        if not cone:
            cone = pdg.upstream_slice(tag)
        sub_waypoints.append(_Waypoint(tag, val, cone))

    sub_waypoints.append(wp)

    logger.info(
        "how: frontier dependency-chain found %d root(s) for %s: %s",
        len(unique_roots),
        wp.tag_name,
        ", ".join(f"{t}={v}" for t, v in unique_roots),
    )
    return _RefinementResult("dependency_chain", sub_waypoints)


def _refine_waypoint(
    frontier_states: list[dict[str, Any]],
    wp: _Waypoint,
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    existing_wp_tags: frozenset[str],
    skip: set[str] | None = None,
    pipeline_cache: Any = None,
) -> _RefinementResult | None:
    """Try refinement strategies in order; return first success."""
    strategies = [
        ("value_gap", _analyze_frontier_value_gap),
        ("condition_blocking", _analyze_frontier_condition_blocking),
        ("dependency_chain", _analyze_frontier_dependency_chain),
    ]
    for name, fn in strategies:
        if skip and name in skip:
            continue
        if fn is _analyze_frontier_value_gap:
            result = fn(
                frontier_states,
                wp,
                snapshot,
                pdg,
                program,
                existing_wp_tags,
                pipeline_cache=pipeline_cache,
            )
        else:
            result = fn(frontier_states, wp, snapshot, pdg, program, existing_wp_tags)
        if result is not None:
            return result
    return None


# Depth cap on the enabler chase in _constraining_from_values.  The SCC under
# decomposition is a cycle by construction, so the chase WILL loop without both
# the `visited` rung set and this bound; the visited set guarantees termination,
# this just keeps the (heuristic, how()-only) search from wandering arbitrarily
# deep through gating chains.  4 covers step → timer → substate → step style
# sequencers; raise if a legitimately deeper enabler chain needs decomposing.
_MAX_ENABLER_CHASE = 4


def _constraining_from_values(
    target_tag: str,
    node: Any,
    program: Any,
    pdg: ProgramGraph,
    depth: int,
    visited: set[int],
) -> set[Any]:
    """Values of *target_tag* that rung *node* (transitively) requires to fire.

    **Direct**: *node*'s own condition constrains *target_tag* — return those
    values.  Otherwise recurse through each condition tag's writer rungs,
    depth-bounded, with *visited* (rung indices) breaking the cycle.  This is
    the multi-hop generalisation of the original one-hop enabler check: a
    ``Step`` advance gated by a ``Timer.done`` that is itself gated by a
    substate that is gated by ``Step`` resolves at depth 2+ instead of failing.

    Heuristic and how()-only — an over- or under-broad result merely yields a
    sub-waypoint whose BFS fails (→ backtrack/fallback) or a spurious value
    path that replay-verify rejects; it never affects soundness.
    """
    from pyrung.core.analysis.simplified import _sp_to_expr

    rung_obj = _resolve_rung(program, node)
    if rung_obj is None:
        return set()
    sp = rung_obj.sp_tree()
    if sp is None:
        return set()
    direct = _extract_condition_values(_sp_to_expr(sp)).get(target_tag)
    if direct:
        return set(direct)
    if depth <= 0:
        return set()
    found: set[Any] = set()
    for cond_tag in node.condition_reads:
        if cond_tag == target_tag:
            continue
        for writer_ri in pdg.writers_of.get(cond_tag, frozenset()):
            if writer_ri in visited:
                continue
            visited.add(writer_ri)
            found |= _constraining_from_values(
                target_tag, pdg.rung_nodes[writer_ri], program, pdg, depth - 1, visited
            )
    return found


def _build_value_transitions(
    tag_name: str,
    pdg: ProgramGraph,
    program: Any,
) -> dict[Any, set[Any]]:
    """Build a value-transition graph for *tag_name*.

    For each writer rung that produces a literal or arithmetic value,
    determines the "from" value by:

    1. **Direct** — the rung's own condition constrains *tag_name* to a
       specific value (e.g. ``with rung(Step == 1): copy(2, Step)``).
    2. **Recursive enabler chase** — the condition mentions another tag
       whose own writer rung (transitively, depth-bounded via
       ``_constraining_from_values``) constrains *tag_name* (e.g. a
       ``Timer.done`` that only runs when ``Step == 1``, possibly through
       intermediate substate gating).

    Returns ``{from_value: {to_values}}``.
    """
    from pyrung.core.analysis.simplified import _sp_to_expr

    transitions: dict[Any, set[Any]] = {}

    for rung_idx in pdg.writers_of.get(tag_name, frozenset()):
        node = pdg.rung_nodes[rung_idx]
        rung_obj = _resolve_rung(program, node)
        if rung_obj is None:
            continue

        wv = _written_value_for_tag(rung_obj, tag_name)
        if wv is None:
            continue

        kind = wv[0]

        # Arithmetic patterns: calc(tag + N, tag) or calc(tag - N, tag)
        if kind in ("increment", "decrement"):
            step = wv[1]
            sp = rung_obj.sp_tree()
            if sp is None:
                continue
            cond_values = _extract_condition_values(_sp_to_expr(sp))
            from_vals = cond_values.get(tag_name) or _constraining_from_values(
                tag_name, node, program, pdg, _MAX_ENABLER_CHASE, {rung_idx}
            )
            if from_vals:
                for fv in from_vals:
                    if isinstance(fv, (int, float)) and isinstance(step, (int, float)):
                        to_val = fv + step if kind == "increment" else fv - step
                        transitions.setdefault(fv, set()).add(to_val)
            continue

        if kind != "literal":
            continue
        to_value = wv[1]

        sp = rung_obj.sp_tree()
        if sp is None:
            continue
        cond_values = _extract_condition_values(_sp_to_expr(sp))

        from_vals = cond_values.get(tag_name) or _constraining_from_values(
            tag_name, node, program, pdg, _MAX_ENABLER_CHASE, {rung_idx}
        )
        if from_vals:
            for fv in from_vals:
                transitions.setdefault(fv, set()).add(to_value)

    return transitions


def _shortest_value_path(
    start: Any,
    goal: Any,
    transitions: dict[Any, set[Any]],
) -> list[Any] | None:
    """BFS over value-transition graph from *start* to *goal*."""
    if start == goal:
        return [start]

    queue: deque[tuple[Any, list[Any]]] = deque([(start, [start])])
    visited: set[Any] = {start}

    while queue:
        current, path = queue.popleft()
        for nxt in transitions.get(current, set()):
            if nxt in visited:
                continue
            new_path = path + [nxt]
            if nxt == goal:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))

    return None


def _stable_step_values(
    step_tag: str,
    values: list[Any],
    compiled: Any,
    dt: float = 0.010,
) -> set[Any]:
    """Return the subset of *values* at which *step_tag* can rest for a scan.

    A counter only "rests" at a value whose advance is *conditional* (it waits
    on an input or sub-state).  Where the advance is *unconditional* — e.g. an
    even step that auto-increments via ``step % 2`` — the value is a pass-through:
    no settled state ever satisfies a waypoint there, so the per-step mini-BFS
    would exhaust and force a fallback.

    The kernel is the oracle.  On a *clean* kernel (all defaults — the quiescent,
    no-command state) set ``step_tag = V`` and run one scan; ``V`` is a rest-state
    iff ``step_tag`` did not move on its own.  General over any auto-advance
    mechanism (modulo, comparator, elapsed timer) — no static gate inversion.

    Deliberately probed from defaults, *not* the planning snapshot: overlaying a
    mid-cycle snapshot onto a fresh kernel re-arms one-shots whose edge memory is
    unset, firing spurious transitions.  how()-only — a misjudgement merely keeps
    or drops a candidate waypoint and is caught by replay-verify.
    """
    from pyrung.core.analysis.prove.kernel import (
        _restore_kernel,
        _snapshot_kernel,
        _step_compiled_kernel,
    )

    kernel = compiled.create_kernel()
    _step_compiled_kernel(compiled, kernel, dt=dt)  # warm-up: spend init one-shots
    warm = _snapshot_kernel(kernel)

    stable: set[Any] = set()
    for v in values:
        _restore_kernel(kernel, warm)
        kernel.tags[step_tag] = v
        _step_compiled_kernel(compiled, kernel, dt=dt)
        if kernel.tags.get(step_tag) == v:
            stable.add(v)
    return stable


def _try_decompose_scc(
    scc: list[str],
    wp_by_tag: dict[str, _Waypoint],
    snapshot: dict[str, Any],
    pdg: ProgramGraph,
    program: Any,
    all_wp_tags: frozenset[str],
    pipeline_cache: Any = None,
    compiled: Any = None,
) -> list[_Waypoint] | None:
    """Try to decompose an SCC mega-waypoint into per-step sub-waypoints.

    Two value-path strategies, per SCC member, decomposing on the longest:

    1. **Static transition graph** (``_build_value_transitions``) — gate
       inversion.  Good for sparse literal-jump machines (``State 0 → 10 → 5``)
       where each write's guard pins the from-value.
    2. **Domain-stepping** — for a monotone ``±1`` counter whose advance gate
       cannot be statically inverted (modulo, comparisons, deep enabler
       chains), the value path is simply its domain sequence
       (``1 → 2 → 3 → …``).  Each per-value sub-waypoint's scoped BFS satisfies
       that one step's gate; correctness is guaranteed by replay-verify, not by
       the static graph.  Gated on an arithmetic writer so a literal-jump tag is
       never stepped through phantom intermediate values.

    Sub-waypoints are marked ``value_stepped`` so the L2 cone-size gate exempts
    them: their cost is search *depth* (one value step), not cone *width*.
    """
    best_sub: list[_Waypoint] | None = None
    best_len = 0

    scc_set = frozenset(scc)
    stop_at = all_wp_tags - scc_set

    for tag in scc:
        wp = wp_by_tag[tag]
        current_value = snapshot.get(tag)
        if current_value == wp.required_value:
            continue

        # Strategy 1: static gate-inversion graph.
        transitions = _build_value_transitions(tag, pdg, program)
        path = (
            _shortest_value_path(current_value, wp.required_value, transitions)
            if transitions
            else None
        )

        # Strategy 2: domain-stepping fallback for ±1 counters when the static
        # graph cannot connect current → target (gate not invertible).
        if (path is None or len(path) <= 2) and _has_arithmetic_writer(tag, pdg, program):
            domain = _get_domain(tag, pipeline_cache)
            if domain is not None:
                dpath = _domain_value_path(current_value, wp.required_value, domain)
                if dpath is not None:
                    path = [current_value, *dpath]

        if path is None or len(path) <= 2:
            continue

        # Drop transient (pass-through) intermediate values.  A waypoint at a
        # value the stepper can't rest at can never be satisfied by a settled
        # state — the mini-BFS would exhaust and force a fallback.  Probe the
        # kernel to keep only rest-states; each surviving per-step BFS then rolls
        # *through* the dropped transients on its way to the next rest-state.
        if compiled is not None and len(path) > 2:
            stable = _stable_step_values(tag, list(path[1:-1]), compiled)
            dropped = [v for v in path[1:-1] if v not in stable]
            path = [path[0], *(v for v in path[1:-1] if v in stable), path[-1]]
            if dropped:
                logger.info(
                    "how: %s value-path %s — dropped transient (pass-through) %s",
                    tag,
                    path,
                    dropped,
                )
            if len(path) <= 2:
                continue

        if len(path) > best_len:
            scc_extras = frozenset(t for t in scc if t != tag)
            sub_waypoints: list[_Waypoint] = []
            for idx in range(1, len(path)):
                intermediate_value = path[idx]
                cone = _value_aware_cone(
                    tag,
                    intermediate_value,
                    pdg,
                    program,
                    stop_at=stop_at,
                    from_value=path[idx - 1],
                )
                if not cone:
                    cone = pdg.upstream_slice(tag)
                cone = cone | scc_extras
                sub_waypoints.append(_Waypoint(tag, intermediate_value, cone, value_stepped=True))
            best_sub = sub_waypoints
            best_len = len(path)

    if best_sub is not None:
        logger.info(
            "how: sub-decomposed SCC {%s} into %d step(s)",
            ", ".join(sorted(scc)),
            len(best_sub),
        )

    return best_sub


def _order_waypoints(
    waypoints: list[_Waypoint],
    pdg: ProgramGraph,
    snapshot: dict[str, Any] | None = None,
    program: Any = None,
    landmark_orderings: dict[Fact, set[Fact]] | None = None,
    actions: list[_Action] | None = None,
    first_achievers: dict[Fact, set[int]] | None = None,
    pipeline_cache: Any = None,
    compiled: Any = None,
) -> list[_Waypoint] | None:
    """Topological sort of waypoints by condition-reads dependency.

    When cyclic dependencies exist (common in state machines where
    StateCurrent ↔ StateRequested), the cycle members are merged into
    a single mega-waypoint with a combined cone so the scoped BFS can
    solve them together.  Non-cyclic waypoints that merely depend on a
    cycle are kept separate and ordered after it.

    Ordering edges come from three sources (merged):
    1. Condition-reads: waypoint A depends on B if A's writer reads B.
    2. Greedy-necessary (from landmark extraction): B is in the shared
       preconditions of all first achievers of A.
    3. Reasonable: achieving A would undo B (all A-achievers require B's
       tag at a different value).
    """
    if len(waypoints) <= 1:
        return waypoints

    wp_tags = {wp.tag_name for wp in waypoints}
    wp_by_tag = {wp.tag_name: wp for wp in waypoints}

    # Source 1: condition-reads edges
    deps: dict[str, set[str]] = {wp.tag_name: set() for wp in waypoints}
    for wp in waypoints:
        for rung_idx in pdg.writers_of.get(wp.tag_name, frozenset()):
            node = pdg.rung_nodes[rung_idx]
            for read_tag in node.condition_reads:
                if read_tag in wp_tags and read_tag != wp.tag_name:
                    deps[wp.tag_name].add(read_tag)

    # Source 2: greedy-necessary orderings from landmark extraction
    if landmark_orderings:
        wp_fact_to_tag = {(wp.tag_name, wp.required_value): wp.tag_name for wp in waypoints}
        for p_fact, predecessors in landmark_orderings.items():
            p_tag = wp_fact_to_tag.get(p_fact)
            if p_tag is None:
                continue
            for q_fact in predecessors:
                q_tag = wp_fact_to_tag.get(q_fact)
                if q_tag is not None and q_tag != p_tag:
                    deps[p_tag].add(q_tag)

    # Source 3: reasonable orderings
    if actions is not None and first_achievers is not None:
        landmarks = {(wp.tag_name, wp.required_value) for wp in waypoints}
        reasonable = _compute_reasonable_orderings(landmarks, actions, first_achievers)
        for q_fact, predecessors in reasonable.items():
            q_tag = wp_fact_to_tag.get(q_fact) if landmark_orderings else None
            if q_tag is None:
                q_tag = next(
                    (wp.tag_name for wp in waypoints if (wp.tag_name, wp.required_value) == q_fact),
                    None,
                )
            if q_tag is None:
                continue
            for p_fact in predecessors:
                p_tag = next(
                    (wp.tag_name for wp in waypoints if (wp.tag_name, wp.required_value) == p_fact),
                    None,
                )
                if p_tag is not None and p_tag != q_tag:
                    deps[q_tag].add(p_tag)

    # --- SCC detection (Tarjan's) to merge only true cycles ---
    sccs = _tarjan_sccs(wp_tags, deps)

    # Merge each multi-member SCC into a single waypoint, keep singletons.
    # Track SCC members for post-sort sub-decomposition.
    scc_wps: list[_Waypoint] = []
    tag_to_scc: dict[str, int] = {}
    mega_members: dict[int, list[str]] = {}
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
            scc_wps.append(
                _Waypoint(
                    wp_by_tag[primary].tag_name,
                    wp_by_tag[primary].required_value,
                    merged_cone,
                )
            )
            mega_members[len(scc_wps) - 1] = list(scc)
            logger.info("how: merged cycle {%s} into mega-waypoint", ", ".join(sorted(scc)))

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
    ordered_indices: list[int] = []

    while ready:
        _, scc_idx = heapq.heappop(ready)
        ordered_indices.append(scc_idx)
        for other, other_deps in scc_deps.items():
            if scc_idx in other_deps:
                other_deps.discard(scc_idx)
                in_degree[other] -= 1
                if in_degree[other] == 0:
                    heapq.heappush(ready, (len(scc_wps[other].cone), other))

    if len(ordered_indices) < len(scc_wps):
        for i in range(len(scc_wps)):
            if i not in ordered_indices:
                ordered_indices.append(i)

    # --- Sub-decompose mega-waypoints by value stepping ---
    all_wp_tags = frozenset(wp.tag_name for wp in waypoints)
    expanded: list[_Waypoint] = []
    for scc_idx in ordered_indices:
        scc = mega_members.get(scc_idx)
        if scc is not None and snapshot is not None and program is not None:
            sub = _try_decompose_scc(
                scc,
                wp_by_tag,
                snapshot,
                pdg,
                program,
                all_wp_tags,
                pipeline_cache=pipeline_cache,
                compiled=compiled,
            )
            if sub is not None:
                expanded.extend(sub)
                continue
        expanded.append(scc_wps[scc_idx])

    return expanded


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

    # L2 fail-fast: a waypoint whose cone is near-program-sized (usually an
    # SCC-merged mega-waypoint) cannot be solved more cheaply than the undecomposed
    # fallback, which has every BFS optimisation and skips the per-waypoint context
    # rebuild.  Skip decomposition entirely so the caller falls back immediately
    # instead of burning the eval budget on a search that can't win.
    oversized = max(
        (len(wp.cone) for wp in waypoints if not wp.probe_expanded and not wp.value_stepped),
        default=0,
    )
    if oversized > _MEGA_CONE_LIMIT:
        logger.info(
            "how: largest waypoint cone=%d exceeds %d-tag limit; skipping "
            "decomposition, falling back to undecomposed BFS",
            oversized,
            _MEGA_CONE_LIMIT,
        )
        return None

    budget_per_wp = max_steps if len(waypoints) == 1 else max(5, max_steps // (len(waypoints) + 1))
    logger.info(
        "how: running %d waypoint(s), budget=%d per waypoint", len(waypoints), budget_per_wp
    )
    current_state = dict(snapshot)
    all_trace_steps: list[Any] = []

    wp_generators: list[Any] = []

    for i, wp in enumerate(waypoints):
        logger.info(
            "how: waypoint %d/%d: %s=%s (cone=%d tags)",
            i + 1,
            len(waypoints),
            wp.tag_name,
            wp.required_value,
            len(wp.cone),
        )
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
            restrict_inputs_to_scope=wp.value_stepped,
        )
        if isinstance(context, Intractable):
            return None

        frontier: list[dict[str, Any]] = []
        gen = _bfs_explore_gen(
            context,
            predicates=[lambda s, _p=wp_pred: not _p(s)],
            depth_budget=budget_per_wp,
            max_states=10_000,
            max_evals=_HOW_EVAL_BUDGET,
            bfs_config=opt.bfs_config,
            initial_state=current_state,
            state_filter=state_filter,
            frontier_collector=frontier,
        )

        result_list = next(gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            logger.info("how: waypoint %d/%d exhausted, backtracking", i + 1, len(waypoints))
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

            if frontier:
                from pyrung.core.analysis.pdg import build_program_graph

                pdg = build_program_graph(program)
                existing_wp_tags = frozenset(w.tag_name for w in waypoints)
                tried_strategies: set[str] = set()
                for _refinement_attempt in range(_MAX_REFINEMENTS):
                    refined = _refine_waypoint(
                        frontier,
                        wp,
                        current_state,
                        pdg,
                        program,
                        existing_wp_tags,
                        skip=tried_strategies,
                        pipeline_cache=pipeline_cache,
                    )
                    if refined is None:
                        break
                    tried_strategies.add(refined.strategy)
                    logger.info(
                        "how: waypoint %d/%d refined via %s (attempt %d)",
                        i + 1,
                        len(waypoints),
                        refined.strategy,
                        _refinement_attempt + 1,
                    )
                    sub_result = _run_refined_waypoints(
                        refined.waypoints,
                        current_state,
                        program,
                        opt,
                        budget_per_wp,
                        compiled=compiled,
                        state_filter=state_filter,
                        pipeline_cache=pipeline_cache,
                    )
                    if sub_result is not None:
                        sub_trace, sub_state = sub_result
                        remaining = waypoints[i + 1 :]
                        if not remaining:
                            all_trace_steps.extend(sub_trace)
                            return all_trace_steps
                        rest = _run_remaining_waypoints(
                            remaining,
                            sub_state,
                            all_trace_steps + sub_trace,
                            program,
                            opt,
                            budget_per_wp,
                            wp_generators,
                            waypoints,
                            snapshot,
                            target_pred,
                            compiled=compiled,
                            state_filter=state_filter,
                            pipeline_cache=pipeline_cache,
                        )
                        if rest is not None:
                            return rest
            elif compiled is not None:
                # Empty frontier: the scoped BFS proved this waypoint unreachable
                # within its static cone — typically because the cone misses a
                # dependency reachable only through indirect addressing (blk[Idx]).
                # Probe the kernel for the hidden inputs and, if the cone genuinely
                # widens, retry this waypoint and the rest with the expanded scope.
                expanded_cone = _probe_cone_expansion(
                    wp.cone,
                    wp.tag_name,
                    compiled,
                    current_state,
                    pipeline_cache,
                    dt=context.dt,
                )
                if len(expanded_cone) > len(wp.cone):
                    logger.info(
                        "how: waypoint %d/%d cone expanded %d -> %d tags via kernel probing",
                        i + 1,
                        len(waypoints),
                        len(wp.cone),
                        len(expanded_cone),
                    )
                    expanded_wp = _Waypoint(
                        wp.tag_name,
                        wp.required_value,
                        expanded_cone,
                        probe_expanded=True,
                    )
                    sub = _run_waypoint_plan(
                        [expanded_wp, *waypoints[i + 1 :]],
                        current_state,
                        target_pred,
                        program,
                        max_steps,
                        opt,
                        compiled=compiled,
                        state_filter=state_filter,
                        pipeline_cache=pipeline_cache,
                    )
                    if sub is not None:
                        return all_trace_steps + sub
            return None

        trace = result_list[0].trace
        wp_generators.append((gen, context, len(all_trace_steps)))

        new_state = _replay_to_state(context.compiled, current_state, trace)
        current_state = new_state
        all_trace_steps.extend(trace)

    return all_trace_steps


def _run_refined_waypoints(
    refined_wps: list[_Waypoint],
    current_state: dict[str, Any],
    program: Any,
    opt: Any,
    budget_per_wp: int,
    compiled: Any = None,
    state_filter: Any = None,
    pipeline_cache: Any = None,
) -> tuple[list[Any], dict[str, Any]] | None:
    """Run a refined waypoint sequence (no backtracking).

    Returns ``(trace_steps, final_state)`` or ``None``.
    """
    from pyrung.core.analysis.prove import _build_explore_context
    from pyrung.core.analysis.prove.bfs import _bfs_explore_gen
    from pyrung.core.analysis.prove.results import Counterexample, Intractable

    all_trace: list[Any] = []
    state = dict(current_state)

    for wp in refined_wps:
        wp_pred = _make_wp_predicate(wp)
        if wp_pred(state):
            continue

        scope = sorted(wp.cone | {wp.tag_name})
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
            max_evals=_HOW_EVAL_BUDGET,
            bfs_config=opt.bfs_config,
            initial_state=state,
            state_filter=state_filter,
        )

        result_list = next(gen, None)
        if result_list is None or not isinstance(result_list[0], Counterexample):
            return None

        trace = result_list[0].trace
        state = _replay_to_state(context.compiled, state, trace)
        all_trace.extend(trace)

    return all_trace, state


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
            max_evals=_HOW_EVAL_BUDGET,
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
