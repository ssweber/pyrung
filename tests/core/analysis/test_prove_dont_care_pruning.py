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


class TestArithmeticOperand:
    """``Lvl == Sp + 2`` read from a tag mapping, checked against what a scan computes."""

    @staticmethod
    def _scan_agrees(cond, values: dict) -> bool | None:
        from pyrung import PLC, Bool, Program, Rung, out
        from pyrung.core.analysis.simplified import _condition_to_expr

        coil = Bool("Coil")
        with Program(strict=False) as program:
            with Rung(cond):
                out(coil)
        plc = PLC(program, dt=0.01)
        plc.patch(values)
        plc.step()
        tags = plc.current_state.tags
        decided = _eval_expr_from_state(_condition_to_expr(cond), tags)
        assert decided is bool(tags["Coil"])
        return decided

    @pytest.mark.parametrize(("level", "expected"), [(0, True), (1, False)])
    def test_division_by_zero_gives_zero(self, level, expected):
        from pyrung import Int

        left, top, bottom = (
            Int("L", external=True),
            Int("Sp", external=True),
            Int("Z", external=True),
        )
        cond = left == top / bottom
        assert self._scan_agrees(cond, {"L": level, "Sp": 5, "Z": 0}) is expected

    @pytest.mark.parametrize(("level", "expected"), [(2, True), (3, False)])
    def test_an_int_over_two_is_true_division(self, level, expected):
        """2 < 5 / 2 holds because 5 / 2 is 2.5, not 2."""
        from pyrung import Int

        left, top = Int("L", external=True), Int("Sp", external=True)
        assert self._scan_agrees(left < top / 2, {"L": level, "Sp": 5}) is expected

    def test_a_tag_the_mapping_lacks_leaves_it_undecided(self):
        from pyrung import Int
        from pyrung.core.analysis.simplified import _condition_to_expr

        expr = _condition_to_expr(Int("L") == Int("Sp") + 2)
        assert _eval_expr_from_state(expr, {"L": 3}) is None
        assert _eval_expr_from_state(expr, {"L": 3, "Sp": 1}) is True


class TestAtomReads:
    """Tests for the tags an atom reads.  A reader must pin or enumerate them before it decides."""

    def test_an_arithmetic_operand_gives_the_tags_it_reads(self):
        from pyrung import Int
        from pyrung.core.analysis.prove.expr import _atom_reads, _referenced_tags
        from pyrung.core.analysis.simplified import _condition_to_expr

        atom = _condition_to_expr(Int("L") == Int("Sp") * Int("K") + 2)
        assert _atom_reads(atom) == {"L", "Sp", "K"}
        assert _referenced_tags(ExprAnd((atom, Atom("Run", "xic")))) == {"L", "Sp", "K", "Run"}

    def test_a_pointer_element_is_a_tag_it_cannot_name(self):
        from pyrung import Block, Int, TagType
        from pyrung.core.analysis.prove.expr import _atom_reads, _referenced_tags
        from pyrung.core.analysis.simplified import _condition_to_expr

        table = Block("Tb", TagType.INT, 1, 3)
        pointer = Int("P")
        compared_with = _condition_to_expr(Int("L") == table[pointer])
        compared = _condition_to_expr(table[pointer] == 5)
        assert _atom_reads(compared_with) is None
        assert _atom_reads(compared) is None
        assert _referenced_tags(ExprAnd((Atom("Run", "xic"), compared))) is None

    def test_a_pinned_gate_refuses_an_operand_tag_it_lacks(self):
        """Pin the gate's contract.  The evaluator's own check already refused before the fix."""
        from pyrung import Int
        from pyrung.core.analysis.pilot.availability import _guard_eval_atom
        from pyrung.core.analysis.simplified import _condition_to_expr

        atom = _condition_to_expr(Int("L") == Int("Sp") + 2)
        assert _guard_eval_atom(atom, {"L": 3}) is None
        assert _guard_eval_atom(atom, {"L": 3, "Sp": 1}) is True
