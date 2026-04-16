"""SP tree types and causal attribution walk for condition structures.

Converts the runtime condition AST (AllCondition/AnyCondition/leaf) into
an SP (series-parallel) tree, then walks the tree to identify which
contacts mattered for a given evaluation — the foundation for causal
chain analysis.

Separate from the Click dialect's ``_topology.py`` SP types: those carry
string labels for codegen; these carry ``Condition`` objects for runtime
attribution.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pyrung.core.condition import Condition


# ---------------------------------------------------------------------------
# SP Tree Types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SPLeaf:
    """A single contact (leaf condition) in the SP tree."""

    condition: Condition


@dataclass(frozen=True)
class SPSeries:
    """AND: all children must be true for the node to be true."""

    children: tuple[SPNode, ...]


@dataclass(frozen=True)
class SPParallel:
    """OR: any child being true makes the node true."""

    children: tuple[SPNode, ...]


SPNode = SPLeaf | SPSeries | SPParallel


# ---------------------------------------------------------------------------
# Attribution result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Attribution:
    """A leaf contact that mattered for a rung's evaluation."""

    condition: Condition
    value: bool  # what the condition evaluated to


# ---------------------------------------------------------------------------
# Condition AST → SP tree conversion
# ---------------------------------------------------------------------------


def _condition_to_sp(condition: Condition) -> SPNode:
    """Convert a single Condition object to an SP node."""
    from pyrung.core.condition import AllCondition, AnyCondition

    if isinstance(condition, AllCondition):
        children = _flatten_children(
            [_condition_to_sp(c) for c in condition.conditions],
            SPSeries,
        )
        if len(children) == 1:
            return children[0]
        return SPSeries(tuple(children))

    if isinstance(condition, AnyCondition):
        children = _flatten_children(
            [_condition_to_sp(c) for c in condition.conditions],
            SPParallel,
        )
        if len(children) == 1:
            return children[0]
        return SPParallel(tuple(children))

    return SPLeaf(condition)


def _flatten_children(
    children: list[SPNode],
    kind: type[SPSeries] | type[SPParallel],
) -> list[SPNode]:
    """Flatten nested same-type nodes (Series-in-Series → single Series)."""
    result: list[SPNode] = []
    for child in children:
        if isinstance(child, kind):
            result.extend(child.children)
        else:
            result.append(child)
    return result


def conditions_to_sp(conditions: list[Condition]) -> SPNode | None:
    """Convert a rung's condition list to an SP tree.

    Args:
        conditions: The rung's ``_conditions`` list.  Multiple entries are
            implicitly ANDed (series).

    Returns:
        The SP tree, or ``None`` for unconditional rungs (empty list).
    """
    if not conditions:
        return None

    nodes = [_condition_to_sp(c) for c in conditions]

    if len(nodes) == 1:
        return nodes[0]

    # Multiple rung-level conditions are implicit AND (series).
    children = _flatten_children(nodes, SPSeries)
    return SPSeries(tuple(children))


# ---------------------------------------------------------------------------
# SP tree evaluation
# ---------------------------------------------------------------------------


def evaluate_sp(
    node: SPNode,
    evaluate: Callable[[Condition], bool],
) -> bool:
    """Evaluate the truth value of an SP tree.

    Args:
        node: The SP tree root.
        evaluate: Callback that returns the truth value of a leaf condition.

    Returns:
        The overall truth value of the tree.
    """
    if isinstance(node, SPLeaf):
        return evaluate(node.condition)

    if isinstance(node, SPSeries):
        return all(evaluate_sp(child, evaluate) for child in node.children)

    # SPParallel
    return any(evaluate_sp(child, evaluate) for child in node.children)


# ---------------------------------------------------------------------------
# Four-rule attribution walk
# ---------------------------------------------------------------------------


def attribute(
    node: SPNode,
    evaluate: Callable[[Condition], bool],
) -> list[Attribution]:
    """Walk an SP tree to find the contacts that mattered for its evaluation.

    Applies four rules post-order:

    +-----------------+------------------------------------------+
    | Node            | Children that mattered                   |
    +-----------------+------------------------------------------+
    | SERIES TRUE     | All children (all were necessary)        |
    | SERIES FALSE    | Only FALSE children (the blockers)       |
    | PARALLEL TRUE   | Only TRUE children (conducting branches) |
    | PARALLEL FALSE  | All children (all were necessary)        |
    +-----------------+------------------------------------------+

    Args:
        node: The SP tree root.
        evaluate: Callback that returns the truth value of a leaf condition.

    Returns:
        Flat list of :class:`Attribution` for every leaf on a "mattered"
        path.  The causal chain engine intersects this with the transition
        log to separate proximate causes from enabling conditions.
    """
    if isinstance(node, SPLeaf):
        return [Attribution(condition=node.condition, value=evaluate(node.condition))]

    if isinstance(node, SPSeries):
        child_values = [(child, evaluate_sp(child, evaluate)) for child in node.children]
        overall = all(v for _, v in child_values)

        result: list[Attribution] = []
        for child, child_val in child_values:
            if overall:
                # SERIES TRUE: all children mattered
                result.extend(attribute(child, evaluate))
            else:
                # SERIES FALSE: only FALSE children mattered
                if not child_val:
                    result.extend(attribute(child, evaluate))
        return result

    # SPParallel
    child_values = [(child, evaluate_sp(child, evaluate)) for child in node.children]
    overall = any(v for _, v in child_values)

    result = []
    for child, child_val in child_values:
        if overall:
            # PARALLEL TRUE: only TRUE children mattered
            if child_val:
                result.extend(attribute(child, evaluate))
        else:
            # PARALLEL FALSE: all children mattered
            result.extend(attribute(child, evaluate))
    return result
