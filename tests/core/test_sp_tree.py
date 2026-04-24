"""Tests for SP tree conversion and four-rule attribution walk."""

from __future__ import annotations

from pyrung.core import And, Bool, Or
from pyrung.core.analysis.sp_tree import (
    SPLeaf,
    SPParallel,
    SPSeries,
    attribute,
    conditions_to_sp,
    evaluate_sp,
)
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    NormallyClosedCondition,
)
from pyrung.core.rung import Rung

# ---------------------------------------------------------------------------
# Conversion: conditions_to_sp
# ---------------------------------------------------------------------------


class TestConditionsToSP:
    """Verify condition AST → SP tree conversion."""

    def test_empty_conditions_returns_none(self) -> None:
        assert conditions_to_sp([]) is None

    def test_single_tag_becomes_leaf(self) -> None:
        A = Bool("A")
        rung = Rung(A)
        tree = rung.sp_tree()

        assert isinstance(tree, SPLeaf)
        assert isinstance(tree.condition, BitCondition)

    def test_two_tags_become_series(self) -> None:
        A = Bool("A")
        B = Bool("B")
        rung = Rung(A, B)
        tree = rung.sp_tree()

        assert isinstance(tree, SPSeries)
        assert len(tree.children) == 2
        assert all(isinstance(c, SPLeaf) for c in tree.children)

    def test_or_becomes_parallel(self) -> None:
        A = Bool("A")
        B = Bool("B")
        rung = Rung(Or(A, B))
        tree = rung.sp_tree()

        assert isinstance(tree, SPParallel)
        assert len(tree.children) == 2

    def test_and_becomes_series(self) -> None:
        A = Bool("A")
        B = Bool("B")
        rung = Rung(And(A, B))
        tree = rung.sp_tree()

        assert isinstance(tree, SPSeries)
        assert len(tree.children) == 2

    def test_nested_and_or(self) -> None:
        """And(A, Or(B, C)) → SPSeries(Leaf(A), SPParallel(Leaf(B), Leaf(C)))."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        rung = Rung(And(A, Or(B, C)))
        tree = rung.sp_tree()

        assert isinstance(tree, SPSeries)
        assert len(tree.children) == 2
        assert isinstance(tree.children[0], SPLeaf)
        assert isinstance(tree.children[1], SPParallel)
        assert len(tree.children[1].children) == 2

    def test_nested_or_and(self) -> None:
        """Or(And(A, B), And(C, D)) → SPParallel(SPSeries(...), SPSeries(...))."""
        A, B, C, D = Bool("A"), Bool("B"), Bool("C"), Bool("D")
        rung = Rung(Or(And(A, B), And(C, D)))
        tree = rung.sp_tree()

        assert isinstance(tree, SPParallel)
        assert len(tree.children) == 2
        assert all(isinstance(c, SPSeries) for c in tree.children)

    def test_negation_becomes_leaf(self) -> None:
        """~A → SPLeaf(NormallyClosedCondition(A))."""
        A = Bool("A")
        rung = Rung(~A)
        tree = rung.sp_tree()

        assert isinstance(tree, SPLeaf)
        assert isinstance(tree.condition, NormallyClosedCondition)

    def test_series_flattening(self) -> None:
        """And(A, And(B, C)) should flatten to SPSeries with 3 children."""
        A, B, C = Bool("A"), Bool("B"), Bool("C")
        rung = Rung(And(A, And(B, C)))
        tree = rung.sp_tree()

        assert isinstance(tree, SPSeries)
        assert len(tree.children) == 3

    def test_parallel_flattening(self) -> None:
        """Or(A, Or(B, C)) should flatten to SPParallel with 3 children."""
        A, B, C = Bool("A"), Bool("B"), Bool("C")
        rung = Rung(Or(A, Or(B, C)))
        tree = rung.sp_tree()

        assert isinstance(tree, SPParallel)
        assert len(tree.children) == 3

    def test_rung_level_and_plus_explicit_and_flatten(self) -> None:
        """Rung(And(A, B), C) should flatten to SPSeries with 3 children."""
        A, B, C = Bool("A"), Bool("B"), Bool("C")
        rung = Rung(And(A, B), C)
        tree = rung.sp_tree()

        assert isinstance(tree, SPSeries)
        assert len(tree.children) == 3

    def test_unconditional_rung(self) -> None:
        rung = Rung()
        assert rung.sp_tree() is None

    def test_single_or_child_unwraps(self) -> None:
        """Or(A) with a single child should unwrap to just a leaf."""
        A = Bool("A")
        tree = conditions_to_sp([AnyCondition(A)])

        assert isinstance(tree, SPLeaf)

    def test_single_and_child_unwraps(self) -> None:
        """And(A) with a single child should unwrap to just a leaf."""
        A = Bool("A")
        tree = conditions_to_sp([AllCondition(A)])

        assert isinstance(tree, SPLeaf)


# ---------------------------------------------------------------------------
# evaluate_sp
# ---------------------------------------------------------------------------


class TestEvaluateSP:
    """Verify SP tree evaluation."""

    def test_leaf_true(self) -> None:
        A = Bool("A")
        cond = BitCondition(A)
        tree = SPLeaf(cond)

        assert evaluate_sp(tree, lambda c: True) is True

    def test_leaf_false(self) -> None:
        A = Bool("A")
        cond = BitCondition(A)
        tree = SPLeaf(cond)

        assert evaluate_sp(tree, lambda c: False) is False

    def test_series_all_true(self) -> None:
        tree = SPSeries((SPLeaf(BitCondition(Bool("A"))), SPLeaf(BitCondition(Bool("B")))))
        assert evaluate_sp(tree, lambda c: True) is True

    def test_series_one_false(self) -> None:
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))
        tree = SPSeries((SPLeaf(A), SPLeaf(B)))

        values = {id(A): True, id(B): False}
        assert evaluate_sp(tree, lambda c: values[id(c)]) is False

    def test_parallel_one_true(self) -> None:
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))
        tree = SPParallel((SPLeaf(A), SPLeaf(B)))

        values = {id(A): True, id(B): False}
        assert evaluate_sp(tree, lambda c: values[id(c)]) is True

    def test_parallel_all_false(self) -> None:
        tree = SPParallel((SPLeaf(BitCondition(Bool("A"))), SPLeaf(BitCondition(Bool("B")))))
        assert evaluate_sp(tree, lambda c: False) is False


# ---------------------------------------------------------------------------
# Four-rule attribution walk
# ---------------------------------------------------------------------------


def _make_tree_and_values():
    """Build the worked example from the spec.

    Series(Sensor_Pressure, Permissive_OK, ~Faulted)

    Scenario: all three TRUE → SERIES TRUE.
    Sensor_Pressure transitioned (proximate), others held (enabling).
    """
    Sensor_Pressure = BitCondition(Bool("Sensor_Pressure"))
    Permissive_OK = BitCondition(Bool("Permissive_OK"))
    Faulted_NC = NormallyClosedCondition(Bool("Faulted"))

    tree = SPSeries((SPLeaf(Sensor_Pressure), SPLeaf(Permissive_OK), SPLeaf(Faulted_NC)))
    values = {id(Sensor_Pressure): True, id(Permissive_OK): True, id(Faulted_NC): True}

    return tree, values, Sensor_Pressure, Permissive_OK, Faulted_NC


class TestAttributionWalk:
    """Verify the four-rule attribution walk."""

    def test_series_true_all_children_matter(self) -> None:
        """SERIES TRUE: all children should appear in attributions."""
        tree, values, sp, po, fn = _make_tree_and_values()

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert sp in conditions
        assert po in conditions
        assert fn in conditions
        assert all(a.value is True for a in result)

    def test_series_false_only_false_children_matter(self) -> None:
        """SERIES FALSE: only the FALSE children should appear."""
        Sensor_Pressure = BitCondition(Bool("Sensor_Pressure"))
        Permissive_OK = BitCondition(Bool("Permissive_OK"))
        Faulted_NC = NormallyClosedCondition(Bool("Faulted"))

        tree = SPSeries((SPLeaf(Sensor_Pressure), SPLeaf(Permissive_OK), SPLeaf(Faulted_NC)))
        # Sensor_Pressure FALSE, others TRUE → series FALSE
        values = {id(Sensor_Pressure): False, id(Permissive_OK): True, id(Faulted_NC): True}

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert Sensor_Pressure in conditions
        assert Permissive_OK not in conditions
        assert Faulted_NC not in conditions
        assert len(result) == 1
        assert result[0].value is False

    def test_series_false_multiple_blockers(self) -> None:
        """SERIES FALSE with two FALSE children: both should appear."""
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))
        C = BitCondition(Bool("C"))

        tree = SPSeries((SPLeaf(A), SPLeaf(B), SPLeaf(C)))
        values = {id(A): False, id(B): True, id(C): False}

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert A in conditions
        assert B not in conditions
        assert C in conditions

    def test_parallel_true_only_true_children_matter(self) -> None:
        """PARALLEL TRUE: only the TRUE children should appear."""
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))

        tree = SPParallel((SPLeaf(A), SPLeaf(B)))
        values = {id(A): True, id(B): False}

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert A in conditions
        assert B not in conditions

    def test_parallel_false_all_children_matter(self) -> None:
        """PARALLEL FALSE: all children should appear."""
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))

        tree = SPParallel((SPLeaf(A), SPLeaf(B)))
        values = {id(A): False, id(B): False}

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert A in conditions
        assert B in conditions

    def test_nested_series_parallel(self) -> None:
        """Series(A, Parallel(B, C)) with A=T, B=T, C=F.

        Series TRUE → all children matter.
        Parallel TRUE → only TRUE children (B) matter.
        Result: A and B, not C.
        """
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))
        C = BitCondition(Bool("C"))

        tree = SPSeries((SPLeaf(A), SPParallel((SPLeaf(B), SPLeaf(C)))))
        values = {id(A): True, id(B): True, id(C): False}

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert A in conditions
        assert B in conditions
        assert C not in conditions

    def test_nested_parallel_series(self) -> None:
        """Parallel(Series(A, B), Series(C, D)) with A=T, B=T, C=F, D=T.

        Parallel TRUE → only TRUE branches matter → Series(A,B).
        Series TRUE → all children matter → A, B.
        Result: A and B, not C or D.
        """
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))
        C = BitCondition(Bool("C"))
        D = BitCondition(Bool("D"))

        tree = SPParallel(
            (
                SPSeries((SPLeaf(A), SPLeaf(B))),
                SPSeries((SPLeaf(C), SPLeaf(D))),
            )
        )
        values = {id(A): True, id(B): True, id(C): False, id(D): True}

        result = attribute(tree, lambda c: values[id(c)])

        conditions = [a.condition for a in result]
        assert A in conditions
        assert B in conditions
        assert C not in conditions
        assert D not in conditions

    def test_single_leaf_attribution(self) -> None:
        """A single leaf always matters."""
        A = BitCondition(Bool("A"))
        tree = SPLeaf(A)

        result = attribute(tree, lambda c: True)
        assert len(result) == 1
        assert result[0].condition is A
        assert result[0].value is True

    def test_attribution_values_reflect_evaluation(self) -> None:
        """Attribution.value should match the actual evaluation result."""
        A = BitCondition(Bool("A"))
        B = BitCondition(Bool("B"))

        tree = SPParallel((SPLeaf(A), SPLeaf(B)))
        values = {id(A): False, id(B): False}

        result = attribute(tree, lambda c: values[id(c)])
        assert all(a.value is False for a in result)
