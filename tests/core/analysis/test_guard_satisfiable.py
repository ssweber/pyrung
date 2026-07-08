"""Unit tests for ``tide_tables.guard_satisfiable``.

The primitive answers "given the values a writer *forces* (``fixed`` — its copy
source, plus any context), could its guard be satisfied by some assignment of the
remaining free operands over their finite domains?"  It is **punt-biased**:

- ``False`` only when the guard is *provably unsatisfiable* over complete finite
  free-tag domains → the caller may soundly reject the writer (producibility);
- ``True`` in every other case — satisfiable, undecidable (a ``None`` term), a
  free tag with no known finite domain, or an enumeration guardrail exceeded.

It generalizes the narrow ``trace._reduce_guard_by_pin`` source-only check; it is
NOT yet wired into the writer-rejection arm (there is no live multi-condition
producibility case demanding it), so these tests pin the primitive in isolation.
"""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, Program, Rung, copy
from pyrung.core.analysis.pdg import build_program_graph
from pyrung.core.analysis.pilot.tide_tables import guard_satisfiable
from pyrung.core.analysis.simplified import And, Atom, Or

# Free-tag domains are supplied explicitly via ``domains=``, so the program only
# has to exist (give a valid pdg); it never needs to write the guard's tags.
_MODE = {"Mode": (1, 2, 3)}


@pytest.fixture
def ctx():
    with Program(strict=False) as prog:
        with Rung():
            copy(0, Int("x"))
        # Referencing the Bool tag as a rung condition registers it in the PDG's
        # ``tags`` map with ``TagType.BOOL`` — needed for the Bool-domain tests
        # below, which resolve a free operand's domain off the tag's declared
        # type rather than an explicit ``domains=`` entry.
        with Rung(Bool("Flag")):
            copy(0, Int("y"))
    return prog, build_program_graph(prog)


def _sat(ctx, expr, fixed, *, domains=None, snapshot=None):
    prog, pdg = ctx
    return guard_satisfiable(
        expr,
        fixed=fixed,
        snapshot=snapshot or {},
        pdg=pdg,
        program=prog,
        domains=domains or {},
    )


# --- Fully-pinned (parity with the narrow source-only check) -----------------


def test_source_only_disequality_violated_is_unsat(ctx):
    """``src != 0`` with the copy pinning ``src == 0`` — the writer can never emit
    it, so provably unsatisfiable."""
    assert _sat(ctx, Atom("src", "ne", 0), {"src": 0}) is False


def test_source_only_disequality_satisfied_is_sat(ctx):
    """``src != 0`` with the pin ``src == 2`` — the pin satisfies the guard."""
    assert _sat(ctx, Atom("src", "ne", 0), {"src": 2}) is True


# --- Multi-tag conjunction (the case the narrow check could not decide) -------


def test_multitag_and_with_satisfiable_partner_is_sat(ctx):
    """``And(src == 2, Mode == 1)`` — ``Mode`` is steerable to a satisfying value,
    so the writer must NOT be rejected."""
    expr = And(terms=(Atom("src", "eq", 2), Atom("Mode", "eq", 1)))
    assert _sat(ctx, expr, {"src": 2}, domains=_MODE) is True


def test_multitag_and_with_unsatisfiable_partner_is_unsat(ctx):
    """``And(src == 2, Mode == 9)`` with ``Mode ∈ {1,2,3}`` — no assignment
    satisfies it, so provably unsatisfiable."""
    expr = And(terms=(Atom("src", "eq", 2), Atom("Mode", "eq", 9)))
    assert _sat(ctx, expr, {"src": 2}, domains=_MODE) is False


# --- Disjunction --------------------------------------------------------------


def test_or_with_one_live_arm_is_sat(ctx):
    """``Or(src == 5, Mode == 1)`` with the pin ``src == 2`` — the ``Mode`` arm is
    steerable, so satisfiable."""
    expr = Or(terms=(Atom("src", "eq", 5), Atom("Mode", "eq", 1)))
    assert _sat(ctx, expr, {"src": 2}, domains=_MODE) is True


def test_or_all_arms_dead_is_unsat(ctx):
    """``Or(src == 5, Mode == 9)`` with ``src == 2`` and ``Mode ∈ {1,2,3}`` — every
    arm is dead over the domains."""
    expr = Or(terms=(Atom("src", "eq", 5), Atom("Mode", "eq", 9)))
    assert _sat(ctx, expr, {"src": 2}, domains=_MODE) is False


# --- Punts (the safe, never-reject direction) --------------------------------


def test_free_tag_without_finite_domain_punts(ctx):
    """A free operand with no resolvable finite domain (a live word) → keep."""
    expr = And(terms=(Atom("src", "eq", 2), Atom("Live", "eq", 7)))
    assert _sat(ctx, expr, {"src": 2}) is True


def test_undecidable_term_punts(ctx):
    """An undecidable term (``rise`` — needs an edge, ``None`` in a single state)
    is never a proof of ``False``, so the guard punts rather than rejects."""
    expr = Or(terms=(Atom("Mode", "rise", True), Atom("Mode", "eq", 9)))
    assert _sat(ctx, expr, {}, domains=_MODE) is True


def test_too_many_free_tags_punts(ctx):
    """More free tags than the enumeration guardrail allows → keep."""
    expr = And(terms=tuple(Atom(t, "eq", 1) for t in ("a", "b", "c", "d")))
    doms = {t: (1,) for t in ("a", "b", "c", "d")}
    assert _sat(ctx, expr, {}, domains=doms) is True


def test_combo_explosion_punts(ctx):
    """A free-domain product past the ``_MAX_COMBOS`` guardrail → keep."""
    expr = And(terms=(Atom("a", "eq", 1), Atom("b", "eq", 1), Atom("c", "eq", 1)))
    doms = {t: tuple(range(20)) for t in ("a", "b", "c")}  # 20^3 = 8000 > 4096
    assert _sat(ctx, expr, {}, domains=doms) is True


# --- Fully-pinned multi-tag (no free operands) -------------------------------


def test_fully_pinned_contradiction_is_unsat(ctx):
    """When every operand is pinned, a definite ``False`` is unsatisfiable."""
    expr = And(terms=(Atom("src", "eq", 2), Atom("Mode", "eq", 1)))
    assert _sat(ctx, expr, {"src": 2, "Mode": 2}) is False


def test_fully_pinned_satisfied_is_sat(ctx):
    """When every operand is pinned and the guard holds, satisfiable."""
    expr = And(terms=(Atom("src", "eq", 2), Atom("Mode", "eq", 1)))
    assert _sat(ctx, expr, {"src": 2, "Mode": 1}) is True


# --- Bool free operand (the gap this fix closes) ------------------------------
#
# ``Flag`` is a real Bool-typed tag registered in the ``ctx`` fixture's PDG (via a
# rung condition), so its domain resolves off the tag's declared type — no
# ``domains=`` entry needed, unlike the synthetic int-domain tags above.


def test_bool_free_operand_unsatisfiable_is_unsat(ctx):
    """``Flag == True`` and ``Flag == False`` can never both hold — no assignment
    over ``(False, True)`` satisfies the conjunction, so provably unsatisfiable."""
    expr = And(terms=(Atom("Flag", "eq", True), Atom("Flag", "eq", False)))
    assert _sat(ctx, expr, {}) is False


def test_bool_free_operand_satisfiable_is_sat(ctx):
    """``Flag == True`` is satisfied by the ``Flag=True`` assignment."""
    expr = Atom("Flag", "eq", True)
    assert _sat(ctx, expr, {}) is True


def test_mixed_bool_and_int_enumeration_is_sat(ctx):
    """``And(Flag == True, Mode == 1)`` — enumerating the Bool domain alongside an
    explicit int domain finds the satisfying combination."""
    expr = And(terms=(Atom("Flag", "eq", True), Atom("Mode", "eq", 1)))
    assert _sat(ctx, expr, {}, domains=_MODE) is True


def test_mixed_bool_and_int_no_combo_satisfies_is_unsat(ctx):
    """``And(Flag == True, Flag == False, Mode == 1)`` — the Bool contradiction
    kills every combo regardless of ``Mode``, so provably unsatisfiable."""
    expr = And(terms=(Atom("Flag", "eq", True), Atom("Flag", "eq", False), Atom("Mode", "eq", 1)))
    assert _sat(ctx, expr, {}, domains=_MODE) is False


def test_bool_operand_combo_cap_counts_toward_guardrail(ctx):
    """The Bool domain's 2 values must be multiplied into the combo-cap product
    like any other free-tag domain: ``2 (Flag) * 2049 (Big) = 4098`` exceeds
    ``_MAX_COMBOS`` (4096) and punts.  If the Bool domain size were *not* counted
    toward the product, 2049 alone would fit under the cap and this would wrongly
    attempt to enumerate instead of punting."""
    expr = And(terms=(Atom("Flag", "eq", True), Atom("Big", "eq", 1)))
    doms = {"Big": tuple(range(2049))}
    assert _sat(ctx, expr, {}, domains=doms) is True
