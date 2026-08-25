"""Shared partial-evaluation walk over simplified boolean expressions.

The prover (`prove/expr.py`) and PILOT (`pilot/trace.py`) both partial-evaluate a
simplified ``And``/``Or``/``Atom``/``Const`` tree against a dict of known tag
values, folding decided atoms into ``Const`` and short-circuit-simplifying the
boolean structure.  The And/Or/Const walk is identical between them; only the
*atom* decision differs (the prover substitutes operand tags manually and calls
``_eval_atom``; PILOT gates on a subset check and calls ``_eval_expr_from_state``).

This module owns the shared skeleton, parameterized by an atom-evaluator
callable so each call site keeps its own atom semantics.  It imports only the
expression types — neither ``prove`` nor ``pilot`` — so both can depend on it
without a cross-dependency.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from pyrung.core.analysis.simplified import And, Atom, Const, Expr, Or

# eval_atom(atom, known) -> bool to fold the atom into ``Const(bool)``, or
# ``None`` to keep the atom as an undecided residual.
AtomEvaluator = Callable[[Atom, dict[str, Any]], "bool | None"]


def partial_eval(expr: Expr, known: dict[str, Any], eval_atom: AtomEvaluator) -> Expr:
    """Substitute known values and short-circuit-simplify And/Or/Const.

    Atom decisions are delegated to *eval_atom*; the boolean walk is shared.
    An ``And`` annihilates on the first ``Const(False)`` term and drops
    ``Const(True)`` terms; an ``Or`` is the dual.  Non-Atom/And/Or/Const nodes
    (e.g. ``ArithAtom``) pass through unchanged.
    """
    if isinstance(expr, Const):
        return expr

    if isinstance(expr, Atom):
        result = eval_atom(expr, known)
        return Const(result) if result is not None else expr

    if isinstance(expr, And):
        terms: list[Expr] = []
        for t in expr.terms:
            evaled = partial_eval(t, known, eval_atom)
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
            evaled = partial_eval(t, known, eval_atom)
            if isinstance(evaled, Const):
                if evaled.value:
                    return Const(True)
                continue
            terms.append(evaled)
        if not terms:
            return Const(False)
        return Or(tuple(terms)) if len(terms) > 1 else terms[0]

    return expr
