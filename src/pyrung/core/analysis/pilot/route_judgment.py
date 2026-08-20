"""Judge completed backward-trace routes without participating in recursion."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import pyrung.core.analysis.pilot.availability as _availability
from pyrung.core.analysis.pilot.trace_tree import TraceNode
from pyrung.core.analysis.sp_values import _values_match

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph


def _is_dead_end_leaf(leaf: TraceNode) -> bool:
    """Whether a terminal backward demand cannot resolve to any action."""

    return (
        not leaf.children
        and not leaf.satisfied
        and not leaf.is_steerable
        and leaf.advance is None
        and not leaf.pipeline_internal
        and not leaf.relational
    )


def route_has_no_dead_end(nodes: list[TraceNode]) -> bool:
    """Whether a completed route contains no unresolved, unactionable leaf."""

    return not any(_is_dead_end_leaf(leaf) for node in nodes for leaf in node.leaves())


def trace_score(nodes: list[TraceNode], pdg: ProgramGraph) -> tuple[int, int, int]:
    """Rank completed routes by downstream reach, pivots, then steerable leaves."""

    steerable = [leaf for node in nodes for leaf in node.leaves() if leaf.is_steerable]
    downstream_reach = sum(
        len(pdg.downstream_slice(leaf.tag, follow_calls=True)) for leaf in steerable
    )
    pivots = sum(node.unsatisfied_count() for node in nodes)
    return downstream_reach, pivots, len(steerable)


def route_forces(nodes: list[TraceNode], snapshot: dict[str, Any], pred: Any) -> bool:
    """Whether the concrete demands across a completed route satisfy ``pred``."""

    overlay = _route_overlay(nodes, snapshot)
    try:
        return bool(pred(overlay))
    except Exception:
        return False


def route_forced_names(
    nodes: list[TraceNode], snapshot: dict[str, Any], avoid: Any
) -> tuple[str, ...]:
    """Return the avoid-condition names satisfied by a route's demands."""

    overlay = _route_overlay(nodes, snapshot)
    violated = getattr(avoid, "violated", None)
    if violated is not None:
        try:
            return tuple(violated(overlay))
        except Exception:
            return ()
    try:
        return ("avoided condition",) if bool(avoid(overlay)) else ()
    except Exception:
        return ()


def _route_overlay(nodes: list[TraceNode], snapshot: dict[str, Any]) -> dict[str, Any]:
    """Overlay one completed route's concrete scalar demands on a snapshot."""

    overlay = dict(snapshot)
    for root in nodes:
        for node in root.iter_nodes():
            if node.relational or node.value is None:
                continue
            overlay[node.tag] = node.value
    return overlay


def _value_sets_intersect(left: Any, right: Any) -> bool:
    """Whether either small value set contains a loosely matching pair."""

    return any(
        _values_match(left_value, right_value) for left_value in left for right_value in right
    )


@dataclass(frozen=True, order=True)
class RouteConflictPin:
    """One stable side of a completed-route conflict witness."""

    values: tuple[str, ...]
    source: tuple[str, int, tuple[str, ...]]


@dataclass(frozen=True, order=True)
class RouteConflict:
    """A concrete pair of incompatible demands on one channel tag."""

    tag: str
    left: RouteConflictPin
    right: RouteConflictPin


def _route_conflict_pin(values: Any, node: TraceNode) -> RouteConflictPin:
    value_keys = tuple(
        sorted(f"{type(value).__module__}.{type(value).__qualname__}:{value!r}" for value in values)
    )
    return RouteConflictPin(
        values=value_keys,
        source=(
            node.tag,
            node.writer_rung if node.writer_rung is not None else -1,
            node.provenance,
        ),
    )


def route_conflicts(
    tree: TraceNode,
    pdg: ProgramGraph,
    program: Any,
) -> frozenset[RouteConflict]:
    """Return simultaneous incompatible demand pairs in one completed route."""

    entries: list[tuple[str, Any, TraceNode, int, frozenset[int]]] = []

    def walk(node: TraceNode, ancestors: frozenset[int]) -> None:
        if not (node.relational or node.value is None):
            alias = _availability._equality_gated_coil(node.tag, node.value, pdg, program)
            demand_tag, demand_values = alias or (node.tag, (node.value,))
            entries.append((demand_tag, demand_values, node, id(node), ancestors))
        child_ancestors = ancestors | {id(node)}
        for child in node.children:
            walk(child, child_ancestors)

    walk(tree, frozenset())

    by_tag: dict[str, list[tuple[Any, TraceNode, int, frozenset[int]]]] = {}
    for tag, values, node, node_id, ancestors in entries:
        by_tag.setdefault(tag, []).append((values, node, node_id, ancestors))

    conflicts: set[RouteConflict] = set()
    for tag, pins in by_tag.items():
        for index, (left_values, left_node, left_id, left_ancestors) in enumerate(pins):
            for right_values, right_node, right_id, right_ancestors in pins[index + 1 :]:
                if _value_sets_intersect(left_values, right_values):
                    continue
                if right_id in left_ancestors or left_id in right_ancestors:
                    continue
                left, right = sorted(
                    (
                        _route_conflict_pin(left_values, left_node),
                        _route_conflict_pin(right_values, right_node),
                    )
                )
                conflicts.add(RouteConflict(tag=tag, left=left, right=right))
    return frozenset(conflicts)
