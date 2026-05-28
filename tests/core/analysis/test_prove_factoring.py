"""Tests for free-input factoring (partition, composition, BFS integration)."""

from __future__ import annotations

from pyrung.core import Bool, Or, Program, Rung, latch, out
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    _build_explore_context,
    prove,
    reachable_states,
)
from pyrung.core.analysis.prove.independence import (
    FreeInputFactoring,
)
from pyrung.core.analysis.prove.passes import _OptConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_factoring(logic: Program) -> FreeInputFactoring | None:
    ctx = _build_explore_context(logic)
    assert not isinstance(ctx, Intractable)
    return ctx.free_input_factoring


# ---------------------------------------------------------------------------
# Unit tests: partition computation
# ---------------------------------------------------------------------------


class TestFreeInputPartition:
    def test_independent_free_inputs_separate_groups(self):
        """Two free inputs with disjoint rungs partition into separate groups."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        factoring = _build_factoring(logic)
        assert factoring is not None
        assert len(factoring.groups) == 2
        all_members = frozenset().union(*factoring.groups)
        assert "A" in all_members
        assert "B" in all_members

    def test_dependent_free_inputs_same_group(self):
        """Two free inputs reading the same rung stay in one group."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(a, b):
                out(x)

        factoring = _build_factoring(logic)
        assert factoring is None

    def test_transitive_dependency_same_group(self):
        """A writes X, rung reads X writes Y, B reads Y → same group."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(x):
                out(y)
            with Rung(y, b):
                out(z)

        factoring = _build_factoring(logic)
        assert factoring is None

    def test_single_free_input_no_factoring(self):
        """One free input produces no factoring (need 2+ groups)."""
        a = Bool("A", external=True)
        x = Bool("X")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)

        factoring = _build_factoring(logic)
        assert factoring is None

    def test_three_inputs_two_independent_one_dependent(self):
        """A and C share a rung → same group; B independent → separate group."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)
            with Rung(x, c):
                out(z)

        factoring = _build_factoring(logic)
        assert factoring is not None
        assert len(factoring.groups) == 2
        for group in factoring.groups:
            if "A" in group:
                assert "C" in group
                assert "B" not in group
            elif "B" in group:
                assert len(group) == 1

    def test_write_tags_populated(self):
        """Each group's write_tags reflects the tags its rungs write."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)

        factoring = _build_factoring(logic)
        assert factoring is not None
        for group, wt in zip(factoring.groups, factoring.write_tags, strict=True):
            if "A" in group:
                assert "X" in wt
            elif "B" in group:
                assert "Y" in wt


# ---------------------------------------------------------------------------
# Integration tests: prove() factored vs unfactored
# ---------------------------------------------------------------------------


class TestFactoringProveIntegration:
    def test_counterexample_agreement(self):
        """prove() gives same verdict with factoring on and off."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        on = prove(logic, ~x, _opt_config=_OptConfig(free_input_factoring=True))
        off = prove(logic, ~x, _opt_config=_OptConfig(free_input_factoring=False))
        assert isinstance(on, Counterexample)
        assert isinstance(off, Counterexample)

    def test_proven_agreement(self):
        """Proven verdict matches with factoring on and off."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        on = prove(logic, Or(~x, ~y), _opt_config=_OptConfig(free_input_factoring=True))
        off = prove(logic, Or(~x, ~y), _opt_config=_OptConfig(free_input_factoring=False))
        assert type(on) is type(off)

    def test_three_independent_free_inputs(self):
        """Three independent free inputs — factoring applies."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)
            with Rung(c):
                latch(z)

        on = prove(logic, ~x, _opt_config=_OptConfig(free_input_factoring=True))
        off = prove(logic, ~x, _opt_config=_OptConfig(free_input_factoring=False))
        assert isinstance(on, Counterexample)
        assert isinstance(off, Counterexample)


# ---------------------------------------------------------------------------
# Integration tests: reachable_states factored vs unfactored
# ---------------------------------------------------------------------------


class TestFactoringReachableStates:
    def test_reachable_states_identical(self):
        """reachable_states produces identical results with factoring on/off."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        on = reachable_states(logic, _opt_config=_OptConfig(free_input_factoring=True))
        off = reachable_states(logic, _opt_config=_OptConfig(free_input_factoring=False))
        assert not isinstance(on, Intractable)
        assert not isinstance(off, Intractable)
        assert on == off

    def test_reachable_states_projected(self):
        """Projected reachable sets are identical with factoring on/off."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        on = reachable_states(
            logic,
            project=["X", "Y"],
            _opt_config=_OptConfig(free_input_factoring=True),
        )
        off = reachable_states(
            logic,
            project=["X", "Y"],
            _opt_config=_OptConfig(free_input_factoring=False),
        )
        assert not isinstance(on, Intractable)
        assert not isinstance(off, Intractable)
        assert on == off

    def test_three_independent_reachable(self):
        """Three independent free inputs — reachable_states identical."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)
            with Rung(c):
                latch(z)

        on = reachable_states(
            logic,
            project=["X", "Y", "Z"],
            _opt_config=_OptConfig(free_input_factoring=True),
        )
        off = reachable_states(
            logic,
            project=["X", "Y", "Z"],
            _opt_config=_OptConfig(free_input_factoring=False),
        )
        assert not isinstance(on, Intractable)
        assert not isinstance(off, Intractable)
        assert on == off


# ---------------------------------------------------------------------------
# Toggle tests
# ---------------------------------------------------------------------------


class TestFactoringToggle:
    def test_toggle_in_opt_config(self):
        cfg_on = _OptConfig(free_input_factoring=True)
        cfg_off = _OptConfig(free_input_factoring=False)
        assert cfg_on.bfs_config.free_input_factoring is True
        assert cfg_off.bfs_config.free_input_factoring is False
        assert "free_input_factoring" in cfg_on.active_optimizations
        assert "free_input_factoring" not in cfg_off.active_optimizations

    def test_in_sound_baseline(self):
        baseline = _OptConfig.sound_baseline()
        assert baseline.free_input_factoring is False

    def test_in_subset(self):
        cfg = _OptConfig().subset({"free_input_factoring"})
        assert cfg.free_input_factoring is True
        assert cfg.traced_elision is False
