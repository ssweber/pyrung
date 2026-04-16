"""Tests for retrospective causal chain analysis (Section C).

Covers the worked example from the spec (Sensor_Pressure → Sts_FaultTripped)
and edge cases for the backward walk algorithm.
"""

from __future__ import annotations

from pyrung.core import PLC, And, Bool, Or, Program, Rung, latch, out, reset

# ---------------------------------------------------------------------------
# Worked example from spec
# ---------------------------------------------------------------------------


def _build_worked_example():
    """Build the six-line ladder fragment from the design spec.

    Rung 0: And(Sensor_Pressure, Permissive_OK, ~Faulted) → latch(Sts_FaultTripped)
    Rung 1: And(Sts_FaultTripped, Cmd_Reset) → reset(Sts_FaultTripped)
    Rung 2: Sts_FaultTripped → out(Alarm_Horn), reset(Cmd_Run)
    """
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


class TestWorkedExample:
    """The worked example from the causal chains spec."""

    def test_fault_tripped_chain(self) -> None:
        """Sensor_Pressure → Sts_FaultTripped chain.

        Scan history (matching spec):
            scan 1: Permissive_OK 0→1
            scan 5: Cmd_Run 0→1
            scan 8: Sensor_Pressure 0→1, Sts_FaultTripped 0→1
            scan 9: Alarm_Horn 0→1, Cmd_Run 1→0
        """
        logic = _build_worked_example()
        runner = PLC(logic)

        # scan 1: Permissive_OK goes TRUE
        runner.patch({"Permissive_OK": True})
        runner.step()

        # scans 2-4: steady state
        for _ in range(3):
            runner.step()

        # scan 5: Cmd_Run goes TRUE
        runner.patch({"Cmd_Run": True})
        runner.step()

        # scans 6-7: steady state
        for _ in range(2):
            runner.step()

        # scan 8: Sensor_Pressure goes TRUE → fault trips
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        # scan 9: Alarm_Horn fires, Cmd_Run resets
        runner.step()

        # Now query: what caused Sts_FaultTripped?
        chain = runner.cause("Sts_FaultTripped")

        assert chain is not None
        assert chain.mode == "retrospective"
        assert chain.effect.tag_name == "Sts_FaultTripped"
        assert chain.effect.from_value is False
        assert chain.effect.to_value is True
        assert chain.effect.scan_id == 8

        # Should have one step: Rung 0 fired
        assert len(chain.steps) >= 1
        step = chain.steps[0]
        assert step.rung_index == 0
        assert step.transition.tag_name == "Sts_FaultTripped"

        # Proximate cause: Sensor_Pressure transitioned at scan 8
        proximate_tags = [p.tag_name for p in step.proximate_causes]
        assert "Sensor_Pressure" in proximate_tags

        # Enabling conditions: Permissive_OK and Faulted held steady
        enabling_tags = [e.tag_name for e in step.enabling_conditions]
        assert "Permissive_OK" in enabling_tags
        assert "Faulted" in enabling_tags

        # Permissive_OK was TRUE, held since scan 1
        perm = next(e for e in step.enabling_conditions if e.tag_name == "Permissive_OK")
        assert perm.value is True
        assert perm.held_since_scan == 1

        # Faulted was FALSE, never transitioned in retained history
        faulted = next(e for e in step.enabling_conditions if e.tag_name == "Faulted")
        assert faulted.value is False
        assert faulted.held_since_scan is None

        # Root causes
        assert len(chain.conjunctive_roots) == 1
        assert chain.conjunctive_roots[0].tag_name == "Sensor_Pressure"

        # Confidence should be 1.0 (unambiguous)
        assert chain.confidence == 1.0

    def test_fault_tripped_at_specific_scan(self) -> None:
        """cause(tag, scan=N) should explain the transition at that scan."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()  # scan 1

        runner.patch({"Sensor_Pressure": True})
        runner.step()  # scan 2: fault trips

        chain = runner.cause("Sts_FaultTripped", scan=2)

        assert chain is not None
        assert chain.effect.scan_id == 2

    def test_alarm_horn_chain(self) -> None:
        """Alarm_Horn chain should trace back through Sts_FaultTripped to Sensor_Pressure."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()  # scan 1

        runner.patch({"Sensor_Pressure": True})
        runner.step()  # scan 2: fault trips, Sts_FaultTripped 0→1

        runner.step()  # scan 3: Alarm_Horn 0→1

        chain = runner.cause("Alarm_Horn")

        assert chain is not None
        assert chain.effect.tag_name == "Alarm_Horn"
        assert chain.effect.to_value is True

        # Step 0: Rung 2 wrote Alarm_Horn because Sts_FaultTripped was TRUE
        assert chain.steps[0].rung_index == 2

        # The chain should have at least 2 steps (Alarm_Horn ← Rung2 ← Sts_FaultTripped ← Rung0)
        assert len(chain.steps) >= 2

        # Root cause should ultimately be Sensor_Pressure
        root_tags = [r.tag_name for r in chain.conjunctive_roots]
        assert "Sensor_Pressure" in root_tags


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    """Edge cases for the backward walk."""

    def test_no_transition_returns_none(self) -> None:
        """cause() on a tag that never changed returns None."""
        X = Bool("X")
        with Program() as logic:
            with Rung():
                out(X)

        runner = PLC(logic)
        runner.step()
        runner.step()

        # X was written but was already False→False initially, so
        # it transitioned on scan 1 from None→False.
        # Let's test a tag that's never been part of the program:
        assert runner.cause("NonExistent") is None

    def test_unconditional_rung(self) -> None:
        """An unconditional rung should produce a step with no proximate causes."""
        X = Bool("X")

        with Program() as logic:
            with Rung():
                latch(X)

        runner = PLC(logic)
        runner.step()

        chain = runner.cause("X")
        assert chain is not None
        # Unconditional rung — the transition is a root
        assert len(chain.conjunctive_roots) >= 1

    def test_tag_object_accepted(self) -> None:
        """cause() should accept a Tag object, not just a string."""
        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)

        runner = PLC(logic)
        runner.patch({"Button": True})
        runner.step()

        chain = runner.cause(Light)
        assert chain is not None
        assert chain.effect.tag_name == "Light"

    def test_two_hop_chain(self) -> None:
        """A → B → C chain should produce 2 steps."""
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with Program() as logic:
            with Rung(A):
                latch(B)
            with Rung(B):
                latch(C)

        runner = PLC(logic)

        # scan 1: A→True, B latches
        runner.patch({"A": True})
        runner.step()

        # scan 2: B is True from scan 1, C latches
        runner.step()

        chain = runner.cause("C")
        assert chain is not None
        assert chain.effect.tag_name == "C"

        # Should trace: C ← Rung1(B) ← Rung0(A)
        rung_indices = chain.rungs()
        assert 1 in rung_indices
        assert 0 in rung_indices

        # Root is A (external input / patch)
        root_tags = [r.tag_name for r in chain.conjunctive_roots]
        assert "A" in root_tags

    def test_or_condition_only_true_branch_matters(self) -> None:
        """With Or(A, B), only the TRUE branch should be proximate."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(Or(A, B)):
                latch(X)

        runner = PLC(logic)

        # Only A is True
        runner.patch({"A": True})
        runner.step()

        chain = runner.cause("X")
        assert chain is not None
        step = chain.steps[0]

        # Only A should be attributed (PARALLEL TRUE → only TRUE children)
        all_tags = [p.tag_name for p in step.proximate_causes] + [
            e.tag_name for e in step.enabling_conditions
        ]
        assert "A" in all_tags
        assert "B" not in all_tags

    def test_scan_specific_no_change_returns_none(self) -> None:
        """cause(tag, scan=N) where tag didn't change at N returns None."""
        X = Bool("X")
        with Program() as logic:
            with Rung():
                out(X)

        runner = PLC(logic)
        runner.step()
        runner.step()

        # X didn't change between scan 1 and scan 2
        assert runner.cause("X", scan=2) is None

    def test_duration_scans(self) -> None:
        """duration_scans should span from earliest step to effect.

        Rung order matters: Rung 0 reads B (before Rung 1 writes it),
        so C transitions one scan after B — a cross-scan chain.
        """
        A = Bool("A")
        B = Bool("B")
        C = Bool("C")

        with Program() as logic:
            with Rung(B):  # Rung 0: reads B (comes before writer)
                latch(C)
            with Rung(A):  # Rung 1: writes B
                latch(B)

        runner = PLC(logic)
        runner.patch({"A": True})
        runner.step()  # scan 1: Rung 0 sees B=False, Rung 1 latches B=True
        runner.step()  # scan 2: Rung 0 sees B=True → latches C

        chain = runner.cause("C")
        assert chain is not None
        assert chain.effect.scan_id == 2
        # Chain spans from scan 1 (A→B) to scan 2 (B→C) → duration = 1
        assert chain.duration_scans == 1


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    """to_dict() and to_config() output."""

    def test_to_dict_structure(self) -> None:
        """to_dict() should have all required keys."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped")
        assert chain is not None
        d = chain.to_dict()

        assert "effect" in d
        assert "mode" in d
        assert "steps" in d
        assert "conjunctive_roots" in d
        assert "ambiguous_roots" in d
        assert "confidence" in d
        assert "duration_scans" in d
        assert d["mode"] == "retrospective"

    def test_to_config_compact(self) -> None:
        """to_config() should be compact with tag names and scan ids."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped")
        assert chain is not None
        c = chain.to_config()

        assert c["effect"] == "Sts_FaultTripped"
        assert "scan" in c
        assert "steps" in c
        assert "confidence" in c


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------


class TestAccessors:
    """tags() and rungs() methods."""

    def test_tags_returns_all_involved(self) -> None:
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped")
        assert chain is not None
        tags = chain.tags()

        assert "Sts_FaultTripped" in tags
        assert "Sensor_Pressure" in tags
        assert "Permissive_OK" in tags
        assert "Faulted" in tags

    def test_rungs_returns_involved_indices(self) -> None:
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped")
        assert chain is not None
        assert 0 in chain.rungs()


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidence:
    """Confidence scoring."""

    def test_unambiguous_is_1(self) -> None:
        """Single proximate cause → confidence 1.0."""
        logic = _build_worked_example()
        runner = PLC(logic)

        runner.patch({"Permissive_OK": True})
        runner.step()
        runner.patch({"Sensor_Pressure": True})
        runner.step()

        chain = runner.cause("Sts_FaultTripped")
        assert chain is not None
        assert chain.confidence == 1.0
        assert chain.ambiguous_roots == []

    def test_conjunctive_roots_dont_reduce_confidence(self) -> None:
        """Multiple simultaneous proximate causes in SERIES are conjunctive, not ambiguous."""
        A = Bool("A")
        B = Bool("B")
        X = Bool("X")

        with Program() as logic:
            with Rung(And(A, B)):
                latch(X)

        runner = PLC(logic)
        # Both A and B transition in the same scan
        runner.patch({"A": True, "B": True})
        runner.step()

        chain = runner.cause("X")
        assert chain is not None
        # Both are conjunctive (fired together), not ambiguous
        assert chain.confidence == 1.0
