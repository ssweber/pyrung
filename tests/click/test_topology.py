"""Unit tests for the shared SP-tree topology module."""

from __future__ import annotations

from pyrung.click._topology import (
    FactorResult,
    Leaf,
    Parallel,
    Series,
    as_series_children,
    factor_outputs,
    flatten,
    make_compound,
    trees_equal,
)


# ---------------------------------------------------------------------------
# flatten
# ---------------------------------------------------------------------------


class TestFlatten:
    def test_flatten_nested_series(self):
        tree = Series([Leaf("A"), Series([Leaf("B"), Leaf("C")])])
        assert flatten(tree, Series) == [Leaf("A"), Leaf("B"), Leaf("C")]

    def test_flatten_nested_parallel(self):
        tree = Parallel([Leaf("A"), Parallel([Leaf("B"), Leaf("C")])])
        assert flatten(tree, Parallel) == [Leaf("A"), Leaf("B"), Leaf("C")]

    def test_flatten_wrong_kind_is_noop(self):
        tree = Series([Leaf("A"), Leaf("B")])
        assert flatten(tree, Parallel) == [tree]

    def test_flatten_leaf(self):
        leaf = Leaf("X")
        assert flatten(leaf, Series) == [leaf]


# ---------------------------------------------------------------------------
# make_compound
# ---------------------------------------------------------------------------


class TestMakeCompound:
    def test_single_child_returns_child(self):
        leaf = Leaf("A")
        assert make_compound([leaf], Series) is leaf

    def test_flattens_nested(self):
        inner = Series([Leaf("B"), Leaf("C")])
        result = make_compound([Leaf("A"), inner], Series)
        assert isinstance(result, Series)
        assert len(result.children) == 3

    def test_sort_key_applied(self):
        a = Leaf("A", row=5)
        b = Leaf("B", row=1)
        c = Leaf("C", row=3)
        result = make_compound([a, b, c], Parallel, sort_key=lambda t: t.row)
        assert isinstance(result, Parallel)
        assert result.children == [b, c, a]

    def test_no_sort_key_preserves_order(self):
        a = Leaf("A", row=5)
        b = Leaf("B", row=1)
        result = make_compound([a, b], Parallel)
        assert isinstance(result, Parallel)
        assert result.children == [a, b]

    def test_sort_key_with_series(self):
        a = Leaf("A", row=5)
        b = Leaf("B", row=1)
        result = make_compound([a, b], Series, sort_key=lambda t: t.row)
        assert isinstance(result, Series)
        assert result.children == [b, a]


# ---------------------------------------------------------------------------
# trees_equal
# ---------------------------------------------------------------------------


class TestTreesEqual:
    def test_none_none(self):
        assert trees_equal(None, None) is True

    def test_none_vs_leaf(self):
        assert trees_equal(None, Leaf("A")) is False
        assert trees_equal(Leaf("A"), None) is False

    def test_same_leaf(self):
        assert trees_equal(Leaf("A"), Leaf("A")) is True

    def test_different_leaf(self):
        assert trees_equal(Leaf("A"), Leaf("B")) is False

    def test_ignores_row_col(self):
        assert trees_equal(Leaf("A", 1, 2), Leaf("A", 3, 4)) is True

    def test_series_vs_parallel(self):
        s = Series([Leaf("A")])
        p = Parallel([Leaf("A")])
        assert trees_equal(s, p) is False

    def test_same_series(self):
        a = Series([Leaf("A"), Leaf("B")])
        b = Series([Leaf("A"), Leaf("B")])
        assert trees_equal(a, b) is True

    def test_different_series_length(self):
        a = Series([Leaf("A")])
        b = Series([Leaf("A"), Leaf("B")])
        assert trees_equal(a, b) is False

    def test_nested_structure(self):
        a = Series([Leaf("A"), Parallel([Leaf("B"), Leaf("C")])])
        b = Series([Leaf("A"), Parallel([Leaf("B"), Leaf("C")])])
        assert trees_equal(a, b) is True


# ---------------------------------------------------------------------------
# as_series_children
# ---------------------------------------------------------------------------


class TestAsSeriesChildren:
    def test_none(self):
        assert as_series_children(None) == []

    def test_leaf(self):
        leaf = Leaf("A")
        assert as_series_children(leaf) == [leaf]

    def test_parallel(self):
        p = Parallel([Leaf("A"), Leaf("B")])
        assert as_series_children(p) == [p]

    def test_series(self):
        children = [Leaf("A"), Leaf("B")]
        s = Series(children)
        result = as_series_children(s)
        assert result == children
        assert result is not s.children  # returns a copy


# ---------------------------------------------------------------------------
# factor_outputs
# ---------------------------------------------------------------------------


class TestFactorOutputs:
    def test_empty(self):
        result = factor_outputs([])
        assert result == FactorResult(shared=[], branches=[])

    def test_single_tree(self):
        tree = Series([Leaf("A"), Leaf("B")])
        result = factor_outputs([tree])
        assert result.shared == [Leaf("A"), Leaf("B")]
        assert result.branches == [[]]

    def test_all_identical(self):
        t1 = Series([Leaf("A"), Leaf("B")])
        t2 = Series([Leaf("A"), Leaf("B")])
        result = factor_outputs([t1, t2])
        assert result.shared == [Leaf("A"), Leaf("B")]
        assert result.branches == [[], []]

    def test_partial_prefix(self):
        t1 = Series([Leaf("A"), Leaf("B"), Leaf("C")])
        t2 = Series([Leaf("A"), Leaf("B"), Leaf("D")])
        result = factor_outputs([t1, t2])
        assert len(result.shared) == 2
        assert trees_equal(result.shared[0], Leaf("A"))
        assert trees_equal(result.shared[1], Leaf("B"))
        assert len(result.branches[0]) == 1
        assert trees_equal(result.branches[0][0], Leaf("C"))
        assert len(result.branches[1]) == 1
        assert trees_equal(result.branches[1][0], Leaf("D"))

    def test_no_shared_prefix(self):
        t1 = Series([Leaf("A"), Leaf("B")])
        t2 = Series([Leaf("X"), Leaf("Y")])
        result = factor_outputs([t1, t2])
        assert result.shared == []
        assert len(result.branches) == 2

    def test_with_none_input(self):
        t1 = Series([Leaf("A"), Leaf("B")])
        result = factor_outputs([t1, None])
        assert result.shared == []
        assert result.branches[0] == [Leaf("A"), Leaf("B")]
        assert result.branches[1] == []

    def test_leaf_inputs(self):
        """Leaf inputs are normalized to single-element series children."""
        result = factor_outputs([Leaf("A"), Leaf("A")])
        assert len(result.shared) == 1
        assert trees_equal(result.shared[0], Leaf("A"))
        assert result.branches == [[], []]

    def test_mixed_prefix_lengths(self):
        t1 = Series([Leaf("A"), Leaf("B")])
        t2 = Leaf("A")
        result = factor_outputs([t1, t2])
        assert len(result.shared) == 1
        assert trees_equal(result.shared[0], Leaf("A"))
        assert len(result.branches[0]) == 1
        assert result.branches[1] == []
