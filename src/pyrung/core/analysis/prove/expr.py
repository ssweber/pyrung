"""Expression helpers for prove state pruning."""

from __future__ import annotations

from typing import Any

from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or


def _collect_atoms_for_tag(exprs: list[Expr], tag_name: str) -> list[Atom]:
    """Collect all Atom nodes referencing a specific tag from a list of expressions."""
    atoms: list[Atom] = []
    for expr in exprs:
        _walk_atoms(expr, tag_name, atoms)
    return atoms


def _walk_atoms(expr: Expr, tag_name: str, out: list[Atom]) -> None:
    if isinstance(expr, Atom):
        if expr.tag == tag_name or expr.operand == tag_name:
            out.append(expr)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_atoms(t, tag_name, out)


def _eval_atom(atom: Atom, value: Any) -> bool | None:
    """Evaluate a single atom against a concrete value.

    Returns None for rise/fall (needs prev — cannot evaluate statically).
    """
    form = atom.form
    if form == "xic":
        return bool(value)
    if form == "xio":
        return not bool(value)
    if form == "truthy":
        return bool(value)
    if form == "rise" or form == "fall":
        return None
    op = atom.operand
    if form == "eq":
        return value == op
    if form == "ne":
        return value != op
    if form == "lt":
        return value < op
    if form == "le":
        return value <= op
    if form == "gt":
        return value > op
    if form == "ge":
        return value >= op
    return None


def _partial_eval(expr: Expr, known: dict[str, Any]) -> Expr:
    """Substitute known tag values and simplify."""
    if isinstance(expr, Const):
        return expr

    if isinstance(expr, Atom):
        if expr.tag in known:
            eval_expr = expr
            if isinstance(expr.operand, str):
                if expr.operand not in known:
                    return expr
                eval_expr = Atom(expr.tag, expr.form, known[expr.operand])
            result = _eval_atom(eval_expr, known[expr.tag])
            if result is not None:
                return Const(result)
        return expr

    if isinstance(expr, And):
        terms: list[Expr] = []
        for t in expr.terms:
            evaled = _partial_eval(t, known)
            if isinstance(evaled, Const):
                if not evaled.value:
                    return Const(False)
                continue
            terms.append(evaled)
        if not terms:
            return Const(True)
        return And(tuple(terms)) if len(terms) > 1 else terms[0]

    if isinstance(expr, Or):
        terms = []
        for t in expr.terms:
            evaled = _partial_eval(t, known)
            if isinstance(evaled, Const):
                if evaled.value:
                    return Const(True)
                continue
            terms.append(evaled)
        if not terms:
            return Const(False)
        return Or(tuple(terms)) if len(terms) > 1 else terms[0]

    return expr


def _referenced_tags(expr: Expr) -> frozenset[str]:
    """Collect all tag names still referenced in an expression."""
    tags: set[str] = set()
    _walk_tags(expr, tags)
    return frozenset(tags)


def _walk_tags(expr: Expr, out: set[str]) -> None:
    if isinstance(expr, Atom):
        out.add(expr.tag)
        if isinstance(expr.operand, str):
            out.add(expr.operand)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_tags(t, out)


def _live_inputs(
    state: dict[str, Any],
    nd_dims: dict[str, tuple[Any, ...]],
    all_exprs: list[Expr],
) -> frozenset[str]:
    """Determine which nondeterministic inputs are live at a given state."""
    nd_names = frozenset(nd_dims)
    known = {k: v for k, v in state.items() if k not in nd_names}

    live: set[str] = set()
    for expr in all_exprs:
        residual = _partial_eval(expr, known)
        if isinstance(residual, Const):
            continue
        live.update(_referenced_tags(residual) & nd_names)

    return frozenset(live)


def _has_edge_atom(expr: Expr, tag_name: str) -> bool:
    """True if *expr* contains a rise/fall atom for *tag_name*."""
    if isinstance(expr, Atom):
        return expr.tag == tag_name and expr.form in ("rise", "fall")
    if isinstance(expr, (And, Or)):
        return any(_has_edge_atom(t, tag_name) for t in expr.terms)
    return False
