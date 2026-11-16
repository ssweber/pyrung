"""Tests for the static facts that how() steers with."""

from __future__ import annotations

import pytest

from pyrung.core.analysis.prove.expr import _eval_atom, _eval_expr_from_state
from pyrung.core.analysis.simplified import And as ExprAnd
from pyrung.core.analysis.simplified import ArithAtom, Atom

# ===================================================================
# Group 3: Don't-care pruning
# ===================================================================


class TestDontCarePruning:
    def test_eval_expr_from_state_handles_arith_atom(self):
        expr = ArithAtom("A", "+", "B", "ge", 5)

        assert _eval_expr_from_state(expr, {"A": 2, "B": 3}) is True
        assert _eval_expr_from_state(expr, {"A": 1, "B": 3}) is False
        assert _eval_expr_from_state(expr, {"A": 2}) is None
        assert _eval_expr_from_state(expr, {"A": True, "B": 3}) is None

    @pytest.mark.parametrize("form", ["eq", "ne", "lt", "le", "gt", "ge"])
    def test_eval_expr_from_state_leaves_a_tag_operand_with_no_value_unknown(self, form):
        expr = Atom("A", form, "B", operand_is_tag=True)

        assert _eval_expr_from_state(expr, {"A": 2}) is None
        assert _eval_expr_from_state(expr, {"A": 2, "B": 2}) is (form in ("eq", "le", "ge"))

    def test_eval_expr_from_state_keeps_edge_atoms_unknown(self):
        expr = ExprAnd((Atom("Ready", "xic"), Atom("Pulse", "rise")))

        assert _eval_expr_from_state(expr, {"Ready": True, "Pulse": True}) is None
        assert _eval_expr_from_state(expr, {"Ready": False, "Pulse": True}) is False

    def test_eval_atom_xic(self):
        assert _eval_atom(Atom("X", "xic"), True) is True
        assert _eval_atom(Atom("X", "xic"), False) is False

    def test_eval_atom_xio(self):
        assert _eval_atom(Atom("X", "xio"), True) is False
        assert _eval_atom(Atom("X", "xio"), False) is True

    def test_eval_atom_eq(self):
        assert _eval_atom(Atom("X", "eq", 5), 5) is True
        assert _eval_atom(Atom("X", "eq", 5), 3) is False

    def test_eval_expr_preserves_tag_operand_provenance(self):
        from pyrung.core.analysis.prove.expr import _eval_expr_from_state

        state = {"State": 1, "DETECTING": 1}
        tag_operand = Atom("State", "eq", "DETECTING", operand_is_tag=True)
        literal_operand = Atom("State", "eq", "DETECTING")

        assert _eval_expr_from_state(tag_operand, state) is True
        assert _eval_expr_from_state(literal_operand, state) is False

    def test_eval_atom_rise_returns_none(self):
        assert _eval_atom(Atom("X", "rise"), True) is None
