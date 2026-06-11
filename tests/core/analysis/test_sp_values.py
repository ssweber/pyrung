"""Tests for the shared static value-extraction helpers (analysis/sp_values).

Moved from the legacy waypoint-planner tests when ``prove/waypoints.py``
was deleted — these helpers survived as the neutral home both the corridor
walker and the prover's seeding import.
"""

from __future__ import annotations

from pyrung import Bool, Int, Program, Rung, calc, copy
from pyrung.core.analysis.simplified import And, Atom, Const, Or
from pyrung.core.analysis.sp_values import (
    _extract_condition_values,
    _extract_required_values,
    _written_value_for_tag,
)


class TestExtractRequiredValues:
    def test_xic_atom(self):
        pairs = _extract_required_values(Atom("Running", "xic"), {})
        assert pairs == [("Running", True)]

    def test_xio_atom(self):
        pairs = _extract_required_values(Atom("Running", "xio"), {})
        assert pairs == [("Running", False)]

    def test_eq_atom(self):
        pairs = _extract_required_values(Atom("State", "eq", 5), {})
        assert pairs == [("State", 5)]

    def test_and_expr(self):
        expr = And((Atom("A", "xic"), Atom("B", "xio")))
        pairs = _extract_required_values(expr, {})
        assert pairs is not None
        assert ("A", True) in pairs
        assert ("B", False) in pairs

    def test_or_picks_cheapest_branch(self):
        expr = Or((Atom("A", "xic"), Atom("B", "xic")))
        snapshot = {"A": True, "B": False}
        pairs = _extract_required_values(expr, snapshot)
        assert pairs == [("A", True)]

    def test_rise_fall_returns_none(self):
        assert _extract_required_values(Atom("X", "rise"), {}) is None
        assert _extract_required_values(Atom("X", "fall"), {}) is None

    def test_const_returns_empty(self):
        assert _extract_required_values(Const(True), {}) == []


class TestExtractConditionValues:
    def test_xic_atom(self):
        assert _extract_condition_values(Atom("X", "xic")) == {"X": frozenset([True])}

    def test_eq_atom(self):
        assert _extract_condition_values(Atom("State", "eq", 5)) == {"State": frozenset([5])}

    def test_and_collects_all(self):
        expr = And((Atom("A", "xic"), Atom("State", "eq", 3)))
        assert _extract_condition_values(expr) == {"A": frozenset([True]), "State": frozenset([3])}

    def test_rise_omitted(self):
        assert _extract_condition_values(Atom("X", "rise")) == {}

    def test_or_same_tag_unions_values(self):
        expr = Or((Atom("A", "eq", 1), Atom("A", "eq", 2)))
        assert _extract_condition_values(expr) == {"A": frozenset([1, 2])}

    def test_or_disjoint_tags_returns_empty(self):
        expr = Or((Atom("A", "xic"), Atom("B", "xic")))
        assert _extract_condition_values(expr) == {}

    def test_or_with_uninvertible_branch_returns_empty(self):
        expr = Or((Atom("A", "xic"), Atom("B", "rise")))
        assert _extract_condition_values(expr) == {}

    def test_and_with_uninvertible_partial(self):
        """And with one invertible and one rise term: extracts the invertible one."""
        expr = And((Atom("A", "xic"), Atom("B", "rise")))
        assert _extract_condition_values(expr) == {"A": frozenset([True])}

    def test_const(self):
        assert _extract_condition_values(Const(True)) == {}


class TestWrittenValueIndirect:
    def test_indirect_copy_source_is_not_a_literal(self):
        """``copy(block[ptr], Dest)`` must return None, not
        ``("literal", IndirectRef)`` — an IndirectRef's comparison
        operators build deferred Conditions that raise on truth-testing,
        so handing one out as a literal crashes any consumer that
        compares it (the live-template shape:
        ``copy(ds[sm__jump_target_ds_idx], sm__where2jump)``)."""
        from pyrung.core.memory_block import Block
        from pyrung.core.tag import TagType

        blk = Block("DS", TagType.INT, 1, 10)
        Ptr = Int("Ptr", default=1)
        Gate = Bool("Gate", external=True)
        Dest = Int("Dest")
        with Program() as prog:
            with Rung(Gate):
                copy(blk[Ptr], Dest)
        rung = prog.rungs[0]

        assert _written_value_for_tag(rung, "Dest") is None

    def test_scalar_copy_source_still_literal(self):
        Gate = Bool("Gate", external=True)
        Dest = Int("Dest")
        with Program() as prog:
            with Rung(Gate):
                copy(7, Dest)
        rung = prog.rungs[0]

        assert _written_value_for_tag(rung, "Dest") == ("literal", 7)


class TestWrittenValueArithmetic:
    def test_calc_increment_detected(self):
        """calc(Step + 1, Step) returns ('increment', 1)."""
        Step = Int("Step")
        Enable = Bool("Enable", external=True)
        with Program() as prog:
            with Rung(Enable, Step == 0):
                calc(Step + 1, Step)
        rung = prog.rungs[0]

        wv = _written_value_for_tag(rung, "Step")
        assert wv is not None
        assert wv[0] == "increment"
        assert wv[1] == 1

    def test_calc_decrement_detected(self):
        """calc(Step - 1, Step) returns ('decrement', 1)."""
        Step = Int("Step")
        Enable = Bool("Enable", external=True)
        with Program() as prog:
            with Rung(Enable, Step == 5):
                calc(Step - 1, Step)
        rung = prog.rungs[0]

        wv = _written_value_for_tag(rung, "Step")
        assert wv is not None
        assert wv[0] == "decrement"
        assert wv[1] == 1
