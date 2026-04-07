"""Shared SP-tree topology types and prefix-factoring helpers.

Owned by the Click dialect; used by both the codegen analyzer (import
from ladder CSV) and the ladder exporter (export to ladder CSV).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, NamedTuple

# ---------------------------------------------------------------------------
# SP Tree Types
# ---------------------------------------------------------------------------


@dataclass
class Leaf:
    """A single condition token in the SP tree."""

    label: Any
    row: int = 0
    col: int = 0


@dataclass
class Series:
    """AND: children evaluated left-to-right."""

    children: list[Leaf | Series | Parallel]


@dataclass
class Parallel:
    """OR: children evaluated top-to-bottom."""

    children: list[Leaf | Series | Parallel]


SPNode = Leaf | Series | Parallel


# ---------------------------------------------------------------------------
# Tree Helpers
# ---------------------------------------------------------------------------


def flatten(
    node: SPNode,
    kind: type[Series] | type[Parallel],
) -> list[SPNode]:
    """Flatten nested Series/Parallel nodes into a single list."""
    if isinstance(node, kind):
        result: list[SPNode] = []
        for child in node.children:
            result.extend(flatten(child, kind))
        return result
    return [node]


def make_compound(
    children: Sequence[SPNode],
    kind: type[Series] | type[Parallel],
    *,
    sort_key: Callable[[SPNode], Any] | None = None,
) -> SPNode:
    """Create a flattened Series/Parallel node.

    When *sort_key* is provided the flattened children are sorted before
    wrapping.  The caller controls ordering — pass ``None`` to preserve
    insertion order.
    """
    flat: list[SPNode] = []
    for child in children:
        flat.extend(flatten(child, kind))
    if len(flat) == 1:
        return flat[0]
    if sort_key is not None:
        flat.sort(key=sort_key)
    return kind(flat)


def trees_equal(a: SPNode | None, b: SPNode | None) -> bool:
    """Structural equality of two SP trees (labels only, ignoring row/col)."""
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if type(a) is not type(b):
        return False
    if isinstance(a, Leaf) and isinstance(b, Leaf):
        return a.label == b.label
    if isinstance(a, (Series, Parallel)) and isinstance(b, (Series, Parallel)):
        if len(a.children) != len(b.children):
            return False
        return all(trees_equal(ac, bc) for ac, bc in zip(a.children, b.children, strict=True))
    return False


def as_series_children(t: SPNode | None) -> list[SPNode]:
    """Normalize any SP tree to a list of series children.

    ``Leaf`` → ``[Leaf]``, ``Parallel`` → ``[Parallel]``,
    ``Series`` → its children, ``None`` → ``[]``.
    """
    if t is None:
        return []
    if isinstance(t, Series):
        return list(t.children)
    return [t]


# ---------------------------------------------------------------------------
# Shared-Prefix Factoring
# ---------------------------------------------------------------------------


class FactorResult(NamedTuple):
    """Result of one-level shared-prefix factoring."""

    shared: list[SPNode]
    """Common prefix (series children)."""

    branches: list[list[SPNode]]
    """Per-output remainders, parallel to the input list."""


def factor_outputs(trees: list[SPNode | None]) -> FactorResult:
    """One-level shared-prefix factoring of SP trees.

    Normalizes each tree to series children, finds the longest common
    prefix (by structural equality of labels), and returns the shared
    prefix and per-tree remainders.
    """
    if not trees:
        return FactorResult(shared=[], branches=[])

    child_lists = [as_series_children(t) for t in trees]

    # Any empty child list means no shared prefix is possible.
    if not all(child_lists):
        return FactorResult(shared=[], branches=child_lists)

    # All-identical fast path.
    if len(trees) > 1 and all(trees_equal(trees[0], t) for t in trees[1:]):
        return FactorResult(shared=child_lists[0], branches=[[] for _ in trees])

    min_len = min(len(cl) for cl in child_lists)
    shared_count = 0
    for i in range(min_len):
        ref = child_lists[0][i]
        if all(trees_equal(ref, cl[i]) for cl in child_lists[1:]):
            shared_count += 1
        else:
            break

    if shared_count == 0:
        return FactorResult(shared=[], branches=child_lists)

    return FactorResult(
        shared=child_lists[0][:shared_count],
        branches=[cl[shared_count:] for cl in child_lists],
    )
