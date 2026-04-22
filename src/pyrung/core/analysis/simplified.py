"""Simplified Boolean form per terminal tag.

For every terminal, resolves the SP-tree condition chain transitively
back to inputs, simplifies the resulting Boolean expression, and renders
it as a human-readable formula.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from pyrung.core.analysis.pdg import TagRole, build_program_graph
from pyrung.core.analysis.sp_tree import SPLeaf, SPNode, SPSeries

if TYPE_CHECKING:
    from pyrung.core.analysis.pdg import ProgramGraph
    from pyrung.core.program import Program
    from pyrung.core.rung import Rung


# ---------------------------------------------------------------------------
# Expression types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Atom:
    """Leaf: a single contact or comparison."""

    tag: str
    form: str  # "xic"|"xio"|"rise"|"fall"|"truthy"|"eq"|"ne"|"lt"|"le"|"gt"|"ge"
    operand: Any = None

    def _key(self) -> tuple[str, str, Any]:
        return (self.tag, self.form, self.operand)


@dataclass(frozen=True)
class And:
    """Conjunction of terms."""

    terms: tuple[Expr, ...]


@dataclass(frozen=True)
class Or:
    """Disjunction of terms."""

    terms: tuple[Expr, ...]


@dataclass(frozen=True)
class Const:
    """Boolean constant (unconditional rung or annihilated expression)."""

    value: bool


Expr = Atom | And | Or | Const


@dataclass(frozen=True)
class TerminalForm:
    """Resolved Boolean expression for one terminal tag."""

    tag: str
    expr: Expr
    writer_count: int
    pivot_count: int
    depth: int

    def __str__(self) -> str:
        return f"{self.tag} = {render(self.expr)}"


# ---------------------------------------------------------------------------
# Condition → Expr conversion
# ---------------------------------------------------------------------------

_COMPARE_FORMS = {
    "CompareEq": "eq",
    "CompareNe": "ne",
    "CompareLt": "lt",
    "CompareLe": "le",
    "CompareGt": "gt",
    "CompareGe": "ge",
}

_INDIRECT_COMPARE_FORMS = {
    "IndirectCompareEq": "eq",
    "IndirectCompareNe": "ne",
    "IndirectCompareLt": "lt",
    "IndirectCompareLe": "le",
    "IndirectCompareGt": "gt",
    "IndirectCompareGe": "ge",
}


def _operand_label(value: Any) -> Any:
    """Render a comparison operand for display."""
    from pyrung.core.tag import Tag

    if isinstance(value, Tag):
        return value.name
    return value


def _condition_to_expr(condition: Any) -> Expr:
    """Convert a Condition object to an Expr."""
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        BitCondition,
        FallingEdgeCondition,
        IntTruthyCondition,
        NormallyClosedCondition,
        RisingEdgeCondition,
    )
    from pyrung.core.tag import ImmediateRef

    if isinstance(condition, AllCondition):
        children = tuple(_condition_to_expr(c) for c in condition.conditions)
        return And(children) if len(children) > 1 else children[0]

    if isinstance(condition, AnyCondition):
        children = tuple(_condition_to_expr(c) for c in condition.conditions)
        return Or(children) if len(children) > 1 else children[0]

    if isinstance(condition, BitCondition):
        tag = condition.tag
        if isinstance(tag, ImmediateRef):
            tag = tag.value
        return Atom(tag.name, "xic")

    if isinstance(condition, NormallyClosedCondition):
        tag = condition.tag
        if isinstance(tag, ImmediateRef):
            tag = tag.value
        return Atom(tag.name, "xio")

    if isinstance(condition, RisingEdgeCondition):
        tag = condition.tag
        if isinstance(tag, ImmediateRef):
            tag = tag.value
        return Atom(tag.name, "rise")

    if isinstance(condition, FallingEdgeCondition):
        tag = condition.tag
        if isinstance(tag, ImmediateRef):
            tag = tag.value
        return Atom(tag.name, "fall")

    if isinstance(condition, IntTruthyCondition):
        return Atom(condition.tag.name, "truthy")

    cls_name = type(condition).__name__

    if cls_name in _COMPARE_FORMS:
        return Atom(
            condition.tag.name,
            _COMPARE_FORMS[cls_name],
            _operand_label(condition.value),
        )

    if cls_name in _INDIRECT_COMPARE_FORMS:
        return Atom(
            f"indirect({cls_name})",
            _INDIRECT_COMPARE_FORMS[cls_name],
            _operand_label(condition.value),
        )

    return Atom(cls_name, "xic")


def _sp_to_expr(node: SPNode) -> Expr:
    """Convert an SP tree to an Expr."""
    if isinstance(node, SPLeaf):
        return _condition_to_expr(node.condition)

    if isinstance(node, SPSeries):
        children = tuple(_sp_to_expr(c) for c in node.children)
        return And(children) if len(children) > 1 else children[0]

    children = tuple(_sp_to_expr(c) for c in node.children)
    return Or(children) if len(children) > 1 else children[0]


# ---------------------------------------------------------------------------
# Rung mapping (node_index → Rung object)
# ---------------------------------------------------------------------------


def _build_rung_map(program: Program) -> dict[int, Rung]:
    """Build node_index → Rung mapping, mirroring build_program_graph order."""
    mapping: dict[int, Rung] = {}
    index = 0

    def walk(rung: Rung) -> None:
        nonlocal index
        mapping[index] = rung
        index += 1
        for branch_rung in rung._branches:
            walk(branch_rung)

    for rung in program.rungs:
        walk(rung)

    for sub_name in sorted(program.subroutines):
        for rung in program.subroutines[sub_name]:
            walk(rung)

    return mapping


# ---------------------------------------------------------------------------
# Writer expression builder (shared by terminal + pivot resolution)
# ---------------------------------------------------------------------------


def _conditions_list_to_expr(conditions: list[Any]) -> Expr:
    """Convert a flat list of Condition objects to an Expr (implicit AND)."""
    if not conditions:
        return Const(True)
    exprs = tuple(_condition_to_expr(c) for c in conditions)
    return exprs[0] if len(exprs) == 1 else And(exprs)


def _try_factored_branches(
    effective: list[int],
    graph: ProgramGraph,
    rung_map: dict[int, Rung],
) -> Expr | None:
    """Factor sibling branches into ``And(parent, Or(local₁, local₂, ...))``.

    Returns ``None`` if the writers are not all sibling branches at
    the same nesting depth.
    """
    nodes = [graph.rung_nodes[ni] for ni in effective]
    if not all(n.branch_path for n in nodes):
        return None

    rungs: list[Rung] = []
    for ni in effective:
        rung = rung_map.get(ni)
        if rung is None:
            return None
        rungs.append(rung)

    starts = [r._branch_condition_start for r in rungs]
    if len(set(starts)) != 1:
        return None

    start = starts[0]
    parent_expr = _conditions_list_to_expr(rungs[0]._conditions[:start])

    local_exprs: list[Expr] = []
    for rung in rungs:
        local_exprs.append(_conditions_list_to_expr(rung._conditions[start:]))

    inner = local_exprs[0] if len(local_exprs) == 1 else Or(tuple(local_exprs))
    return And((parent_expr, inner))


def _expr_for_writers(
    writer_indices: frozenset[int],
    graph: ProgramGraph,
    rung_map: dict[int, Rung],
    *,
    before: int | None = None,
) -> tuple[Expr, list[int]] | None:
    """Build the combined Expr for a tag's writers.

    Groups writer node indices by top-level rung_index, keeps only the
    last rung group (OTE last-write-wins).  When all writers in the group
    are sibling branches, the shared parent conditions are factored out
    (``And(parent, Or(local₁, local₂))``); otherwise branches are ORed.

    *before*, when set, restricts to writers whose node index < before.

    Returns ``(expr, effective_node_indices)`` or ``None`` if no writers.
    """
    indices = writer_indices
    if before is not None:
        indices = frozenset(i for i in indices if i < before)
        if not indices:
            indices = writer_indices

    by_rung: dict[int, list[int]] = {}
    for ni in indices:
        node = graph.rung_nodes[ni]
        by_rung.setdefault(node.rung_index, []).append(ni)

    last_rung_index = max(by_rung)
    effective = sorted(by_rung[last_rung_index])

    if len(effective) > 1:
        factored = _try_factored_branches(effective, graph, rung_map)
        if factored is not None:
            return factored, effective

    branch_exprs: list[Expr] = []
    for ni in effective:
        rung = rung_map.get(ni)
        if rung is None:
            continue
        sp = rung.sp_tree()
        if sp is None:
            branch_exprs.append(Const(True))
        else:
            branch_exprs.append(_sp_to_expr(sp))

    if not branch_exprs:
        return None

    expr = branch_exprs[0] if len(branch_exprs) == 1 else Or(tuple(branch_exprs))
    return expr, effective


# ---------------------------------------------------------------------------
# Pivot resolution
# ---------------------------------------------------------------------------

_MAX_DEPTH = 50


def _ote_resolvable(graph: ProgramGraph) -> frozenset[str]:
    """Return pivot tags where every writer rung uses OutInstruction (OTE).

    Only OTE writes have combinational semantics (tag = rung condition).
    Latch/reset, timers, counters, and copy are stateful — their tags
    cannot be reduced to a Boolean expression of the rung condition.
    """
    resolvable: set[str] = set()
    for tag_name, role in graph.tag_roles.items():
        if role != TagRole.PIVOT:
            continue
        writer_indices = graph.writers_of.get(tag_name, frozenset())
        if not writer_indices:
            continue
        if all(tag_name in graph.rung_nodes[ni].ote_writes for ni in writer_indices):
            resolvable.add(tag_name)
    return frozenset(resolvable)


def _resolve_pivots(
    expr: Expr,
    graph: ProgramGraph,
    rung_map: dict[int, Rung],
    *,
    resolvable: frozenset[str],
    reader_node_index: int | None = None,
    visited: frozenset[str] = frozenset(),
    depth: int = 0,
    _stats: dict[str, int] | None = None,
) -> Expr:
    """Recursively substitute pivot atoms with their writing rung's expression.

    Only pivots in *resolvable* (all writers are OTE) are substituted.
    """
    if depth >= _MAX_DEPTH:
        return expr

    if isinstance(expr, Const):
        return expr

    if isinstance(expr, And):
        resolved = tuple(
            _resolve_pivots(
                t,
                graph,
                rung_map,
                resolvable=resolvable,
                reader_node_index=reader_node_index,
                visited=visited,
                depth=depth,
                _stats=_stats,
            )
            for t in expr.terms
        )
        return And(resolved)

    if isinstance(expr, Or):
        resolved = tuple(
            _resolve_pivots(
                t,
                graph,
                rung_map,
                resolvable=resolvable,
                reader_node_index=reader_node_index,
                visited=visited,
                depth=depth,
                _stats=_stats,
            )
            for t in expr.terms
        )
        return Or(resolved)

    assert isinstance(expr, Atom)
    tag_name = expr.tag

    if tag_name not in resolvable:
        return expr

    if tag_name in visited:
        return expr

    if expr.form not in ("xic", "xio"):
        return expr

    writer_indices = graph.writers_of.get(tag_name, frozenset())
    if not writer_indices:
        return expr

    result = _expr_for_writers(
        writer_indices,
        graph,
        rung_map,
        before=reader_node_index,
    )
    if result is None:
        return expr

    pivot_expr, effective = result

    if _stats is not None:
        _stats["pivot_count"] = _stats.get("pivot_count", 0) + 1
        _stats["depth"] = max(_stats.get("depth", 0), depth + 1)

    resolved = _resolve_pivots(
        pivot_expr,
        graph,
        rung_map,
        resolvable=resolvable,
        reader_node_index=max(effective),
        visited=visited | {tag_name},
        depth=depth + 1,
        _stats=_stats,
    )

    if expr.form == "xio":
        resolved = _negate(resolved)

    return resolved


def _negate(expr: Expr) -> Expr:
    """Wrap an expression in logical negation (push into atoms where possible)."""
    if isinstance(expr, Const):
        return Const(not expr.value)

    if isinstance(expr, Atom):
        flips = {"xic": "xio", "xio": "xic"}
        if expr.form in flips:
            return Atom(expr.tag, flips[expr.form], expr.operand)
        return Atom(expr.tag, expr.form, expr.operand)

    # De Morgan for compound expressions
    if isinstance(expr, And):
        return Or(tuple(_negate(t) for t in expr.terms))

    if isinstance(expr, Or):
        return And(tuple(_negate(t) for t in expr.terms))

    return expr  # pragma: no cover


# ---------------------------------------------------------------------------
# Simplification
# ---------------------------------------------------------------------------


def _expr_key(expr: Expr) -> tuple[Any, ...]:
    """Stable sort key for deduplication and canonical ordering."""
    if isinstance(expr, Const):
        return (0, expr.value)
    if isinstance(expr, Atom):
        return (1, expr.tag, expr.form, str(expr.operand))
    if isinstance(expr, And):
        return (2, tuple(_expr_key(t) for t in expr.terms))
    if isinstance(expr, Or):
        return (3, tuple(_expr_key(t) for t in expr.terms))
    return (9,)  # pragma: no cover


def simplify(expr: Expr) -> Expr:
    """Simplify a Boolean expression via algebraic rules.

    Runs to a fixed point: flatten, dedup, identity, annihilation,
    absorption, single-child unwrap.
    """
    for _ in range(20):
        reduced = _simplify_once(expr)
        if reduced == expr:
            return reduced
        expr = reduced
    return expr


def _simplify_once(expr: Expr) -> Expr:
    if isinstance(expr, (Const, Atom)):
        return expr

    if isinstance(expr, And):
        terms = _flatten_and(expr)
        terms = _dedup(terms)
        terms = _remove_identity(terms, Const(True))
        if any(isinstance(t, Const) and not t.value for t in terms):
            return Const(False)
        terms = _absorb(terms, And, Or)
        if not terms:
            return Const(True)
        if len(terms) == 1:
            return terms[0]
        return And(tuple(terms))

    if isinstance(expr, Or):
        terms = _flatten_or(expr)
        terms = _dedup(terms)
        terms = _remove_identity(terms, Const(False))
        if any(isinstance(t, Const) and t.value for t in terms):
            return Const(True)
        terms = _absorb(terms, Or, And)
        if not terms:
            return Const(False)
        if len(terms) == 1:
            return terms[0]
        return Or(tuple(terms))

    return expr  # pragma: no cover


def _flatten_and(expr: And) -> list[Expr]:
    result: list[Expr] = []
    for t in expr.terms:
        t = _simplify_once(t)
        if isinstance(t, And):
            result.extend(t.terms)
        else:
            result.append(t)
    return result


def _flatten_or(expr: Or) -> list[Expr]:
    result: list[Expr] = []
    for t in expr.terms:
        t = _simplify_once(t)
        if isinstance(t, Or):
            result.extend(t.terms)
        else:
            result.append(t)
    return result


def _dedup(terms: list[Expr]) -> list[Expr]:
    seen: set[tuple[Any, ...]] = set()
    result: list[Expr] = []
    for t in terms:
        key = _expr_key(t)
        if key not in seen:
            seen.add(key)
            result.append(t)
    return result


def _remove_identity(terms: list[Expr], identity: Const) -> list[Expr]:
    return [t for t in terms if t != identity]


def _absorb(
    terms: list[Expr],
    outer_type: type[And] | type[Or],
    inner_type: type[Or] | type[And],
) -> list[Expr]:
    """Absorption: Or(a, And(a, b)) → a; And(a, Or(a, b)) → a."""
    atom_keys = {_expr_key(t) for t in terms if isinstance(t, (Atom, Const))}
    if not atom_keys:
        return terms

    result: list[Expr] = []
    for t in terms:
        if isinstance(t, inner_type):
            child_keys = {_expr_key(c) for c in t.terms}
            if child_keys & atom_keys:
                continue
        result.append(t)
    return result


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_OP_SYMBOLS = {
    "eq": "==",
    "ne": "!=",
    "lt": "<",
    "le": "<=",
    "gt": ">",
    "ge": ">=",
}


def render(expr: Expr) -> str:
    """Render an expression as a human-readable string."""
    return _render(expr, parent=None)


def _render(expr: Expr, parent: type | None) -> str:
    if isinstance(expr, Const):
        return "True" if expr.value else "False"

    if isinstance(expr, Atom):
        if expr.form == "xic":
            return expr.tag
        if expr.form == "xio":
            return f"~{expr.tag}"
        if expr.form in ("rise", "fall"):
            return f"{expr.form}({expr.tag})"
        if expr.form == "truthy":
            return f"{expr.tag} != 0"
        if expr.form in _OP_SYMBOLS:
            return f"{expr.tag} {_OP_SYMBOLS[expr.form]} {expr.operand}"
        return expr.tag

    if isinstance(expr, And):
        parts = [_render(t, And) for t in expr.terms]
        inner = ", ".join(parts)
        if parent is not None:
            return f"And({inner})"
        return inner

    if isinstance(expr, Or):
        parts = [_render(t, Or) for t in expr.terms]
        return f"Or({', '.join(parts)})"

    return "?"  # pragma: no cover


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def simplified_forms(program: Program) -> dict[str, TerminalForm]:
    """Compute the simplified Boolean form for every terminal tag."""
    graph = build_program_graph(program)
    rung_map = _build_rung_map(program)
    resolvable = _ote_resolvable(graph)

    results: dict[str, TerminalForm] = {}

    for tag_name, role in sorted(graph.tag_roles.items()):
        if role != TagRole.TERMINAL:
            continue

        writer_indices = graph.writers_of.get(tag_name, frozenset())
        if not writer_indices:
            continue

        result = _expr_for_writers(writer_indices, graph, rung_map)
        if result is None:
            continue

        raw_expr, effective = result

        stats: dict[str, int] = {"pivot_count": 0, "depth": 0}
        resolved = _resolve_pivots(
            raw_expr,
            graph,
            rung_map,
            resolvable=resolvable,
            reader_node_index=max(effective),
            _stats=stats,
        )
        simplified_expr = simplify(resolved)

        results[tag_name] = TerminalForm(
            tag=tag_name,
            expr=simplified_expr,
            writer_count=len(writer_indices),
            pivot_count=stats["pivot_count"],
            depth=stats["depth"],
        )

    return results
