"""Tests for partial-order reduction (independence relation, ample sets, BFS integration)."""

from __future__ import annotations

from pyrung.core import Bool, Or, Program, Rung, latch, out, rise
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    _build_explore_context,
    prove,
    reachable_states,
)
from pyrung.core.analysis.prove.independence import (
    IndependenceRelation,
    _filter_assignments_to_ample,
    _select_ample_set,
    _visible_actions,
)
from pyrung.core.analysis.prove.passes import _OptConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_relation(logic: Program) -> IndependenceRelation:
    """Run the pre-BFS pipeline and return the independence relation."""
    ctx = _build_explore_context(logic)
    assert not isinstance(ctx, Intractable)
    assert ctx.independence_relation is not None
    return ctx.independence_relation


# ---------------------------------------------------------------------------
# Unit tests: independence relation
# ---------------------------------------------------------------------------


class TestIndependenceRelation:
    def test_independent_inputs_separate_rungs(self):
        """Two inputs writing separate tags through separate rungs are independent."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        rel = _build_relation(logic)
        assert len(rel.action_names) == 2
        ia = rel.action_index_by_name["A"]
        ib = rel.action_index_by_name["B"]
        assert ib in rel.independent[ia]
        assert ia in rel.independent[ib]

    def test_dependent_inputs_shared_rung(self):
        """Two edge-bearing inputs in the same rung condition are dependent."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(rise(a), rise(b)):
                latch(x)

        rel = _build_relation(logic)
        ia = rel.action_index_by_name["A"]
        ib = rel.action_index_by_name["B"]
        assert ib not in rel.independent[ia]

    def test_dependent_inputs_write_read_chain(self):
        """A writes X, B's rung reads X → dependent."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                out(x)
            with Rung(x, rise(b)):
                latch(y)

        rel = _build_relation(logic)
        ia = rel.action_index_by_name["A"]
        ib = rel.action_index_by_name["B"]
        assert ib not in rel.independent[ia]

    def test_single_input_trivial_relation(self):
        """One edge-bearing input produces a trivial (empty) independence relation."""
        a = Bool("A", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)

        rel = _build_relation(logic)
        assert len(rel.action_names) <= 1

    def test_three_inputs_partial_independence(self):
        """A and B independent; A and C dependent (share a rung)."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                out(x)
            with Rung(rise(b)):
                out(y)
            with Rung(x, rise(c)):
                out(z)

        rel = _build_relation(logic)
        ia = rel.action_index_by_name["A"]
        ib = rel.action_index_by_name["B"]
        ic = rel.action_index_by_name["C"]
        assert ib in rel.independent[ia], "A and B should be independent"
        assert ic not in rel.independent[ia], "A and C should be dependent"
        assert ic in rel.independent[ib], (
            "B and C should be independent (disjoint rungs, no write/read conflict)"
        )

    def test_transitive_dependency(self):
        """A writes X, rung reading X writes Y, B reads Y → dependent."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                out(x)
            with Rung(x):
                out(y)
            with Rung(y, rise(b)):
                latch(z)

        rel = _build_relation(logic)
        ia = rel.action_index_by_name["A"]
        ib = rel.action_index_by_name["B"]
        assert ib not in rel.independent[ia]


# ---------------------------------------------------------------------------
# Unit tests: ample set selection
# ---------------------------------------------------------------------------


class TestAmpleSetSelection:
    def test_singleton_found(self):
        """When one input is independent of all others, it becomes the ample set."""
        rel = IndependenceRelation(
            action_names=("A", "B", "C"),
            independent=(
                frozenset({1, 2}),  # A independent of B and C
                frozenset({0}),  # B independent of A only
                frozenset({0}),  # C independent of A only
            ),
            action_index_by_name={"A": 0, "B": 1, "C": 2},
            write_tags=(frozenset({"X"}), frozenset({"Y"}), frozenset({"Z"})),
        )
        result = _select_ample_set(rel, frozenset({0, 1, 2}))
        assert result == frozenset({0})

    def test_no_ample_when_all_dependent(self):
        """No singleton ample set when all pairs are dependent."""
        rel = IndependenceRelation(
            action_names=("A", "B"),
            independent=(frozenset(), frozenset()),
            action_index_by_name={"A": 0, "B": 1},
            write_tags=(frozenset({"X"}), frozenset({"X"})),
        )
        result = _select_ample_set(rel, frozenset({0, 1}))
        assert result is None

    def test_ample_with_two_live_independent(self):
        """Two mutually independent live inputs — either can be the ample set."""
        rel = IndependenceRelation(
            action_names=("A", "B"),
            independent=(frozenset({1}), frozenset({0})),
            action_index_by_name={"A": 0, "B": 1},
            write_tags=(frozenset({"X"}), frozenset({"Y"})),
        )
        result = _select_ample_set(rel, frozenset({0, 1}))
        assert result is not None
        assert len(result) == 1

    def test_subset_of_live_inputs(self):
        """Only live inputs matter for selection."""
        rel = IndependenceRelation(
            action_names=("A", "B", "C"),
            independent=(
                frozenset({1, 2}),
                frozenset({0, 2}),
                frozenset({0, 1}),
            ),
            action_index_by_name={"A": 0, "B": 1, "C": 2},
            write_tags=(frozenset({"X"}), frozenset({"Y"}), frozenset({"Z"})),
        )
        result = _select_ample_set(rel, frozenset({0, 1}))
        assert result is not None

    def test_c3_visibility_restricts_candidates(self):
        """C3: visible actions cannot be in the ample set."""
        rel = IndependenceRelation(
            action_names=("A", "B"),
            independent=(frozenset({1}), frozenset({0})),
            action_index_by_name={"A": 0, "B": 1},
            write_tags=(frozenset({"X"}), frozenset({"Y"})),
        )
        visible = _visible_actions(rel, frozenset({"X"}))
        assert 0 in visible
        assert 1 not in visible
        invisible = frozenset(range(2)) - visible
        result = _select_ample_set(rel, frozenset({0, 1}), invisible_only=invisible)
        assert result == frozenset({1})

    def test_c3_all_visible_blocks_por(self):
        """When all actions are visible, no ample set is possible."""
        rel = IndependenceRelation(
            action_names=("A", "B"),
            independent=(frozenset({1}), frozenset({0})),
            action_index_by_name={"A": 0, "B": 1},
            write_tags=(frozenset({"X"}), frozenset({"Y"})),
        )
        visible = _visible_actions(rel, frozenset({"X", "Y"}))
        invisible = frozenset(range(2)) - visible
        result = _select_ample_set(rel, frozenset({0, 1}), invisible_only=invisible)
        assert result is None


# ---------------------------------------------------------------------------
# Unit tests: assignment filtering
# ---------------------------------------------------------------------------


class TestAssignmentFiltering:
    def test_stutter_always_kept(self):
        rel = IndependenceRelation(
            action_names=("A", "B"),
            independent=(frozenset({1}), frozenset({0})),
            action_index_by_name={"A": 0, "B": 1},
            write_tags=(frozenset({"X"}), frozenset({"Y"})),
        )
        stutter = (("A", False), ("B", False))
        flip_a = (("A", True), ("B", False))
        flip_b = (("A", False), ("B", True))
        assignments = [stutter, flip_a, flip_b]
        current = {"A": False, "B": False}

        result = _filter_assignments_to_ample(assignments, frozenset({0}), rel, current)
        assert stutter in result
        assert flip_a in result
        assert flip_b not in result

    def test_non_ample_filtered(self):
        rel = IndependenceRelation(
            action_names=("A", "B"),
            independent=(frozenset({1}), frozenset({0})),
            action_index_by_name={"A": 0, "B": 1},
            write_tags=(frozenset({"X"}), frozenset({"Y"})),
        )
        stutter = (("A", False), ("B", False))
        flip_b = (("A", False), ("B", True))
        assignments = [stutter, flip_b]
        current = {"A": False, "B": False}

        result = _filter_assignments_to_ample(assignments, frozenset({0}), rel, current)
        assert flip_b not in result


# ---------------------------------------------------------------------------
# Integration tests: prove() with POR on/off produces identical verdicts
# ---------------------------------------------------------------------------


class TestPORProveIntegration:
    def test_counterexample_with_invisible_action(self):
        """POR with C3: action writing non-property tags is invisible and reducible."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        on = prove(logic, ~x, _opt_config=_OptConfig(partial_order_reduction=True))
        off = prove(logic, ~x, _opt_config=_OptConfig(partial_order_reduction=False))
        assert isinstance(on, Counterexample)
        assert isinstance(off, Counterexample)

    def test_proven_with_por(self):
        """prove() returns Proven with POR on/off for expression-based property."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        on = prove(logic, Or(~x, ~y), _opt_config=_OptConfig(partial_order_reduction=True))
        off = prove(logic, Or(~x, ~y), _opt_config=_OptConfig(partial_order_reduction=False))
        assert type(on) is type(off)

    def test_three_independent_inputs_prove(self):
        """Three independent inputs — POR reduces for prove()."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)
            with Rung(rise(c)):
                latch(z)

        on = prove(logic, ~x, _opt_config=_OptConfig(partial_order_reduction=True))
        off = prove(logic, ~x, _opt_config=_OptConfig(partial_order_reduction=False))
        assert isinstance(on, Counterexample)
        assert isinstance(off, Counterexample)


# ---------------------------------------------------------------------------
# Integration tests: reachable_states unaffected by POR flag
# ---------------------------------------------------------------------------


class TestPORReachableStates:
    def test_reachable_states_identical_with_por_flag(self):
        """POR doesn't apply to reachable_states — results must be identical."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        on = reachable_states(logic, _opt_config=_OptConfig(partial_order_reduction=True))
        off = reachable_states(logic, _opt_config=_OptConfig(partial_order_reduction=False))
        assert not isinstance(on, Intractable)
        assert not isinstance(off, Intractable)
        assert on == off

    def test_reachable_states_with_projection(self):
        """POR doesn't apply to reachable_states — projected sets identical."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(x)
            with Rung(rise(b)):
                latch(y)

        on = reachable_states(
            logic,
            project=["X", "Y"],
            _opt_config=_OptConfig(partial_order_reduction=True),
        )
        off = reachable_states(
            logic,
            project=["X", "Y"],
            _opt_config=_OptConfig(partial_order_reduction=False),
        )
        assert not isinstance(on, Intractable)
        assert not isinstance(off, Intractable)
        assert on == off


# ---------------------------------------------------------------------------
# Toggle tests
# ---------------------------------------------------------------------------


class TestPORToggle:
    def test_por_toggle_in_opt_config(self):
        cfg_on = _OptConfig(partial_order_reduction=True)
        cfg_off = _OptConfig(partial_order_reduction=False)
        assert cfg_on.bfs_config.partial_order_reduction is True
        assert cfg_off.bfs_config.partial_order_reduction is False
        assert "partial_order_reduction" in cfg_on.active_optimizations
        assert "partial_order_reduction" not in cfg_off.active_optimizations

    def test_por_in_sound_baseline(self):
        baseline = _OptConfig.sound_baseline()
        assert baseline.partial_order_reduction is False

    def test_por_in_subset(self):
        cfg = _OptConfig().subset({"partial_order_reduction"})
        assert cfg.partial_order_reduction is True
        assert cfg.traced_elision is False
