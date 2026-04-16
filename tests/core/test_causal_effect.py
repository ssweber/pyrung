"""Tests for retrospective forward causal chain analysis (effect()).

Covers the worked example from the spec (Sensor_Pressure forward chain),
counterfactual evaluation, steady-state stopping, and edge cases.
"""

from __future__ import annotations

from pyrung.core import PLC, And, Bool, Or, Program, Rung, latch, out, reset

# ---------------------------------------------------------------------------
# Worked example from spec
# ---------------------------------------------------------------------------


def _build_worked_example():
    """Build the six-line ladder fragment from the design spec."""
    Sensor_Pressure = Bool("Sensor_Pressure")
    Permissive_OK = Bool("Permissive_OK")
    Faulted = Bool("Faulted")
    Sts_FaultTripped = Bool("Sts_FaultTripped")
    Cmd_Reset = Bool("Cmd_Reset")
    Alarm_Horn = Bool("Alarm_Horn")
    Cmd_Run = Bool("Cmd_Run")

    with Program() as logic:
        with Rung(And(Sensor_Pressure, Permissive_OK, ~Faulted)):
            latch(Sts_FaultTripped)

        with Rung(And(Sts_FaultTripped, Cmd_Reset)):
            reset(Sts_FaultTripped)

        with Rung(Sts_FaultTripped):
            out(Alarm_Horn)
            reset(Cmd_Run)

    return logic


class TestWorkedExampleEffect:
    """Forward chain from the spec's worked example."""

    def test_sensor_pressure_forward_chain(self) -> None:
        """Sensor_Pressure → Sts_FaultTripped → Alarm_Horn.

        In pyrung's read-after-write model, all three transitions happen
        at the same scan (scan 2).
        """
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()  # scan 1

        runner.patch({"Sensor_Pressure": True})
        runner.step()  # scan 2: all propagate in one scan

        chain = runner.effect("Sensor_Pressure", scan=2)

        assert chain is not None
        assert chain.mode == "retrospective"
        assert chain.effect.tag_name == "Sensor_Pressure"
        assert chain.effect.scan_id == 2

        # Should find downstream effects
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "Sts_FaultTripped" in effect_tags
        assert "Alarm_Horn" in effect_tags

    def test_sts_fault_tripped_forward(self) -> None:
        """Sts_FaultTripped → Alarm_Horn."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.effect("Sts_FaultTripped", scan=2)

        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "Alarm_Horn" in effect_tags


# ---------------------------------------------------------------------------
# Counterfactual evaluation
# ---------------------------------------------------------------------------


class TestCounterfactual:
    """Counterfactual evaluation correctly identifies load-bearing causes."""

    def test_cause_is_load_bearing(self) -> None:
        """When a cause is necessary for the rung, counterfactual detects it."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(And(A, B)):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True, "B": True})
        runner.step()

        chain = runner.effect("A", scan=1)
        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" in effect_tags

    def test_irrelevant_tag_not_in_chain(self) -> None:
        """A tag that transitions but doesn't affect a rung shouldn't appear."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung(A):
                latch(X)
            with Rung(B):
                latch(Y)

        runner = PLC(logic)
        runner.patch({"A": True, "B": True})
        runner.step()

        chain = runner.effect("A", scan=1)
        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" in effect_tags
        # Y is caused by B, not A — counterfactual should exclude it
        assert "Y" not in effect_tags

    def test_or_condition_not_load_bearing(self) -> None:
        """In Or(A, B) with both True, flipping one doesn't change outcome."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(Or(A, B)):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True, "B": True})
        runner.step()

        # A is not load-bearing: Or(False, True) still True
        chain = runner.effect("A", scan=1)
        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" not in effect_tags

    def test_or_condition_sole_true_is_load_bearing(self) -> None:
        """In Or(A, B) with only A True, A is load-bearing."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(Or(A, B)):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})  # B stays False
        runner.step()

        chain = runner.effect("A", scan=1)
        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" in effect_tags


# ---------------------------------------------------------------------------
# Cross-scan propagation
# ---------------------------------------------------------------------------


class TestCrossScanPropagation:
    """Effects that propagate across scan boundaries."""

    def test_cross_scan_effect(self) -> None:
        """Effect detected when reading rung comes before writing rung.

        Rung 0 reads B (before Rung 1 writes it), so C transitions one
        scan after B.
        """
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with Program() as logic:
            with Rung(B):  # Rung 0: reads B
                latch(C)
            with Rung(A):  # Rung 1: writes B
                latch(B)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()  # scan 1: B latches (Rung 1), C not yet (Rung 0 ran first)
        runner.step()  # scan 2: C latches (Rung 0 sees B=True)

        chain = runner.effect("A", scan=1)
        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "B" in effect_tags
        assert "C" in effect_tags

    def test_duration_spans_scans(self) -> None:
        """Forward chain duration should reflect the scan span."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with Program() as logic:
            with Rung(B):
                latch(C)
            with Rung(A):
                latch(B)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()  # scan 1: B
        runner.step()  # scan 2: C

        chain = runner.effect("A", scan=1)
        assert chain is not None
        assert chain.duration_scans >= 1


# ---------------------------------------------------------------------------
# Steady-state stopping
# ---------------------------------------------------------------------------


class TestSteadyStateStopping:
    """Verify the forward walk stops at steady state."""

    def test_stops_after_k_empty_scans(self) -> None:
        """Walk should stop after K consecutive scans with no new effects."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()  # scan 1: X latches

        # Run several more scans to create history for the walk
        for _ in range(5):
            runner.step()

        chain = runner.effect("A", scan=1, steady_state_k=2)
        assert chain is not None
        # Chain should find X and stop — not walk forever
        assert len(chain.steps) >= 1

    def test_no_transition_returns_none(self) -> None:
        """effect() on a tag that never changed returns None."""
        with Program() as logic:
            with Rung():
                out(Bool("X"))

        runner = PLC(logic)
        runner.step()
        runner.step()

        assert runner.effect("NonExistent") is None

    def test_max_scans_cap(self) -> None:
        """Walk respects max_scans hard cap."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        # With max_scans=1, should still find immediate effects
        chain = runner.effect("A", scan=1, max_scans=1)
        assert chain is not None


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEffectEdgeCases:
    """Edge cases for the forward walk."""

    def test_tag_object_accepted(self) -> None:
        """effect() should accept a Tag object, not just a string."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        chain = runner.effect(A, scan=1)
        assert chain is not None
        assert chain.effect.tag_name == "A"

    def test_most_recent_transition(self) -> None:
        """effect(tag) without scan= should find the most recent transition."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        chain = runner.effect("A")
        assert chain is not None
        assert chain.effect.tag_name == "A"

    def test_unconditional_rung_no_effects(self) -> None:
        """An unconditional rung doesn't pass the counterfactual — no SP tree."""
        A = Bool("A")
        X = Bool("X")
        Y = Bool("Y")

        with Program() as logic:
            with Rung():  # Unconditional — always fires
                latch(X)
            with Rung(A):  # Conditional — uses A so it's part of the program
                latch(Y)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        # X is written unconditionally, so A's transition isn't load-bearing for X
        chain = runner.effect("A", scan=1)
        assert chain is not None
        effect_tags = [s.transition.tag_name for s in chain.steps]
        assert "X" not in effect_tags
        # Y IS caused by A
        assert "Y" in effect_tags

    def test_serialization(self) -> None:
        """to_dict() and to_config() work on effect chains."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        chain = runner.effect("A", scan=1)
        assert chain is not None

        d = chain.to_dict()
        assert d["mode"] == "retrospective"
        assert "steps" in d

        c = chain.to_config()
        assert c["effect"] == "A"

    def test_tags_and_rungs_accessors(self) -> None:
        """tags() and rungs() work on effect chains."""
        A = Bool("A")
        X = Bool("X")

        with Program() as logic:
            with Rung(A):
                latch(X)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()

        chain = runner.effect("A", scan=1)
        assert chain is not None
        assert "A" in chain.tags()
        assert "X" in chain.tags()
        assert 0 in chain.rungs()
