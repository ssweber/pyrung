"""Tests for free-input factoring (partition, composition, BFS integration)."""

from __future__ import annotations

from pyrung.core import Bool, Int, Or, Program, Rung, Word, calc, latch, out, receive
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    _build_explore_context,
    always,
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

    def test_input_writing_other_input_same_group(self):
        """A free input whose cone *writes* another free input's own tag is not
        independent of it — even though that tag has no readers of its own.

        The canonical case is a receive() dest gated by another input: the dest
        is a written-yet-nondeterministic tag, so it has an empty influence cone
        (nothing reads it) and the writes-vs-reads checks can't catch the
        coupling. Regression for soundness_20260528_190313_002.
        """
        gate = Bool("Gate", external=True)
        acc = Int("Acc", min=0, max=3)
        dest = Int("Dest", min=0, max=3)
        busy, ok, err, code = Bool("Busy"), Bool("OK"), Bool("Err"), Int("Code")

        with Program(strict=False) as logic:
            with Rung(gate):
                calc(acc + 1, acc)
            with Rung(acc):
                receive(
                    target="device1",
                    remote_start="DS1",
                    dest=dest,
                    receiving=busy,
                    success=ok,
                    error=err,
                    exception_response=code,
                )

        # Gate -> Acc -> gates receive -> writes Dest, so Gate's cone writes the
        # free input Dest: they collapse to a single group (no factoring).
        factoring = _build_factoring(logic)
        assert factoring is None


# ---------------------------------------------------------------------------
# Integration tests: always() factored vs unfactored
# ---------------------------------------------------------------------------


class TestFactoringProveIntegration:
    def test_counterexample_agreement(self):
        """always() gives same verdict with factoring on and off."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        on = always(logic, ~x, _opt_config=_OptConfig(free_input_factoring=True))
        off = always(logic, ~x, _opt_config=_OptConfig(free_input_factoring=False))
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

        on = always(logic, Or(~x, ~y), _opt_config=_OptConfig(free_input_factoring=True))
        off = always(logic, Or(~x, ~y), _opt_config=_OptConfig(free_input_factoring=False))
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

        on = always(logic, ~x, _opt_config=_OptConfig(free_input_factoring=True))
        off = always(logic, ~x, _opt_config=_OptConfig(free_input_factoring=False))
        assert isinstance(on, Counterexample)
        assert isinstance(off, Counterexample)

    def test_receive_dest_gated_by_input_no_false_proof(self):
        """Factoring must not split a receive() dest from the input that gates it.

        ``In0`` increments ``N0``; ``N0`` gates a receive() into ``W0``. ``W0``
        is nondeterministic (receive dest) but also written, so its influence
        cone is empty and earlier factoring wrongly judged In0 ⊥ W0. Evaluating
        them in separate groups dropped the (In0 low → receive never fires →
        injected W0 survives) states and falsely proved ``W0 < 22``.

        Regression for soundness_20260528_190313_002.
        """
        In0 = Bool("In0", external=True)
        N0 = Int("N0")
        W0 = Word("W0")
        busy, ok, err, code = Bool("RxBusy"), Bool("RxOK"), Bool("RxErr"), Int("RxCode")

        with Program(strict=False) as logic:
            with Rung(In0):
                calc(N0 + 1, N0)
            with Rung(N0):
                receive(
                    target="device1",
                    remote_start="DS1",
                    dest=W0,
                    receiving=busy,
                    success=ok,
                    error=err,
                    exception_response=code,
                )

        on = always(
            logic,
            W0 < 22,
            max_states=10_000,
            depth_budget=20,
            _opt_config=_OptConfig(free_input_factoring=True),
        )
        off = always(
            logic,
            W0 < 22,
            max_states=10_000,
            depth_budget=20,
            _opt_config=_OptConfig(free_input_factoring=False),
        )
        # W0 can be received as 22 while In0 stays low, so W0 < 22 is violable.
        assert isinstance(off, Counterexample)
        assert isinstance(on, Counterexample)


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
