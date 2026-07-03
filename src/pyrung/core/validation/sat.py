"""Static satisfiability primitives for the rung/comparison validators.

A thin public surface over the interval/domain solver that already lives in
:mod:`pyrung.core.validation._common`:

* :func:`conjunction_satisfiable` — is the AND of leaf Conditions feasible?
* :func:`negate_leaf` — the logical complement of a leaf Condition (Condition-level
  twin of ``simplified._negate``'s atom flips).
* :func:`disjunction_tautological` — is ``Or(*terms)`` provably always-true?  Proven
  by De Morgan onto the conjunction solver, so no disjunction reasoning is needed in
  the domain machinery.

The RUNG_* and CMP_* rules build on these, and the De Morgan repair-hint reuses
``negate_leaf`` + the two sat checks to test a rung's And/Or dual.

For now :func:`conjunction_satisfiable` delegates to the private
``_common._conjunction_satisfiable``; the solver body moves here wholesale when the
registry refactor lands (see ``scratchpad/validator-refactor-plan.md`` §4.1).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pyrung.core.condition import (
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    NormallyClosedCondition,
)
from pyrung.core.validation._common import _conjunction_satisfiable

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyrung.core.condition import Condition


def conjunction_satisfiable(conditions: Iterable[Condition]) -> bool:
    """True when the AND of leaf *conditions* has a feasible assignment.

    Groups leaf conditions by tag and checks per-tag domain feasibility.  Opaque
    forms (edges, indirect compares, ``AnyCondition``) are treated as satisfiable
    — conservative by design (never a false "unsatisfiable").
    """
    return _conjunction_satisfiable(conditions)


# Complementary comparison classes — the Condition-level twin of
# ``simplified._negate``'s atom-form flips.  Symmetric, so one dict covers both
# directions.
_COMPARE_COMPLEMENT: dict[type, type] = {
    CompareEq: CompareNe,
    CompareNe: CompareEq,
    CompareLt: CompareGe,
    CompareGe: CompareLt,
    CompareLe: CompareGt,
    CompareGt: CompareLe,
}


def negate_leaf(cond: Condition) -> Condition | None:
    """The logical complement of a leaf Condition, or ``None`` if opaque.

    ``None`` means "cannot negate statically" — edges (rise/fall), indirect and
    arithmetic compares, and the compound ``AllCondition``/``AnyCondition``.
    Callers treat that conservatively (see :func:`disjunction_tautological`).
    """
    if isinstance(cond, (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)):
        return _COMPARE_COMPLEMENT[type(cond)](cond.tag, cond.value)
    if isinstance(cond, BitCondition):
        return NormallyClosedCondition(cond.tag)
    if isinstance(cond, NormallyClosedCondition):
        return BitCondition(cond.tag)
    return None


def disjunction_tautological(terms: Iterable[Condition]) -> bool:
    """True when ``Or(*terms)`` is provably always-true.

    Uses De Morgan: ``Or(t1..tn) ≡ True  ⟺  And(¬t1..¬tn)`` is unsatisfiable.
    The canonical case is ``Or(x != a, x != b, x != c)`` over one variable, whose
    negation ``And(x == a, x == b, x == c)`` is an impossible multi-equality pin.

    Opaque terms (``negate_leaf`` → ``None``) are **dropped**, not failed: proving
    ``Or`` tautological over a subset proves it for the whole (extra disjuncts only
    add truth).  An empty surviving set ⇒ not provably tautological — conservative,
    zero false positives.
    """
    negated = [n for t in terms if (n := negate_leaf(t)) is not None]
    if not negated:
        return False
    return not conjunction_satisfiable(negated)
