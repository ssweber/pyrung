"""Expression helpers for the static facts and PILOT."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pyrung.core.analysis.simplified import And, ArithAtom, Atom, Const, Expr, Or
from pyrung.core.expression import Expression
from pyrung.core.memory_block import IndirectBlockRange, IndirectExprRef, IndirectRef


def _build_atom_index(exprs: list[Expr]) -> dict[str, list[Atom]]:
    """Build a tag-name → atoms index in a single pass over all expressions."""
    index: dict[str, list[Atom]] = {}
    for expr in exprs:
        _index_atoms(expr, index)
    return index


def _index_atoms(expr: Expr, index: dict[str, list[Atom]]) -> None:
    if isinstance(expr, Atom):
        index.setdefault(expr.tag, []).append(expr)
        if expr.operand_is_tag and expr.operand != expr.tag:
            index.setdefault(expr.operand, []).append(expr)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _index_atoms(t, index)


def _collect_atoms_for_tag(atom_index: Mapping[str, list[Atom]], tag_name: str) -> list[Atom]:
    """Look up all Atom nodes referencing a specific tag."""
    return atom_index.get(tag_name, [])


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


def _eval_atom_from_state(atom: Atom, state: Mapping[str, Any]) -> bool | None:
    """Evaluate one atom against a concrete tag mapping."""
    if atom.form in {"rise", "fall"}:
        return None
    if atom.tag not in state:
        return None

    eval_atom = atom
    if atom.operand_is_tag:
        # An operand tag the state lacks leaves the atom undecided, as a missing left-hand tag
        # does.  Comparing with the operand's name would give a wrong answer or raise TypeError.
        if atom.operand not in state:
            return None
        try:
            operand = atom.operand_scale * state[atom.operand] + atom.operand_offset
        except TypeError:
            return None
        eval_atom = Atom(atom.tag, atom.form, operand)
    elif isinstance(atom.operand, (Expression, IndirectRef, IndirectExprRef, IndirectBlockRange)):
        # An expression or a pointer element answers a comparison with a condition, not a
        # bool, and the mapping cannot say what it reads.
        return None
    return _eval_atom(eval_atom, state[atom.tag])


def _eval_arith_atom_from_state(atom: ArithAtom, state: Mapping[str, Any]) -> bool | None:
    """Evaluate one arithmetic comparison against a concrete tag mapping."""
    if atom.left not in state or atom.right not in state:
        return None
    left = state[atom.left]
    right = state[atom.right]
    if (
        not isinstance(left, (int, float))
        or isinstance(left, bool)
        or not isinstance(right, (int, float))
        or isinstance(right, bool)
    ):
        return None
    try:
        if atom.arith_op == "+":
            actual = left + right
        elif atom.arith_op == "-":
            actual = left - right
        elif atom.arith_op == "*":
            actual = left * right
        else:
            return None
        return _eval_atom(Atom("", atom.form, atom.operand), actual)
    except TypeError:
        return None


def _eval_expr_from_state(expr: Expr | ArithAtom, state: Mapping[str, Any]) -> bool | None:
    """Evaluate an expression against a concrete tag mapping.

    Returns ``None`` when residual edge-sensitive or missing-tag terms make the
    truth value undecidable from the mapping alone.
    """
    if isinstance(expr, Const):
        return bool(expr.value)
    if isinstance(expr, Atom):
        return _eval_atom_from_state(expr, state)
    if isinstance(expr, ArithAtom):
        return _eval_arith_atom_from_state(expr, state)
    if isinstance(expr, And):
        saw_unknown = False
        for term in expr.terms:
            result = _eval_expr_from_state(term, state)
            if result is False:
                return False
            if result is None:
                saw_unknown = True
        return None if saw_unknown else True
    if isinstance(expr, Or):
        saw_unknown = False
        for term in expr.terms:
            result = _eval_expr_from_state(term, state)
            if result is True:
                return True
            if result is None:
                saw_unknown = True
        return None if saw_unknown else False
    return None


def _referenced_tags(expr: Expr) -> frozenset[str]:
    """Collect all tag names still referenced in an expression."""
    tags: set[str] = set()
    _walk_tags(expr, tags)
    return frozenset(tags)


def _walk_tags(expr: Expr, out: set[str]) -> None:
    if isinstance(expr, Atom):
        out.add(expr.tag)
        if expr.operand_is_tag:
            out.add(expr.operand)
    elif isinstance(expr, (And, Or)):
        for t in expr.terms:
            _walk_tags(t, out)


def _edge_source_tags(program: Any) -> frozenset[str]:
    from pyrung.core.analysis.simplified import _condition_to_expr
    from pyrung.core.instruction.control import iter_loop_body_rungs

    result: set[str] = set()

    def _walk_expr(expr: Expr) -> None:
        if isinstance(expr, Atom):
            if expr.form in {"rise", "fall"}:
                result.add(expr.tag)
            return
        if isinstance(expr, (And, Or)):
            for term in expr.terms:
                _walk_expr(term)

    def _walk_rung(rung: Any) -> None:
        for condition in rung._conditions:
            _walk_expr(_condition_to_expr(condition))
        for branch in rung._branches:
            _walk_rung(branch)
        for body_rung in iter_loop_body_rungs(rung._instructions):
            _walk_rung(body_rung)

    for rung in program.rungs:
        _walk_rung(rung)
    for rungs in program.subroutines.values():
        for rung in rungs:
            _walk_rung(rung)

    return frozenset(result)
