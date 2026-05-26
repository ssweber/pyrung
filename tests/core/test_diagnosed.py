"""Tests for diagnosed causal chain analysis (snapshot-only backward walk).

Uses the same worked example as test_causal_chain.py but operates on a
frozen snapshot instead of recorded history.  Validates the three branches
(stateless, stateful-cleared, reset path) and multi-tag merging.
"""

from __future__ import annotations

from pyrung.core import PLC, And, Bool, Int, Or, Program, Rung, calc, copy, latch, out, reset
from pyrung.core.state import SystemState

# ---------------------------------------------------------------------------
# Shared program builders
# ---------------------------------------------------------------------------


def _build_worked_example():
    """Same program as test_causal_chain's worked example.

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


def _build_or_example():
    """Program with an OR condition to test ambiguity.

    Rung 0: Or(SensorA, SensorB) → out(Alarm)
    """
    SensorA = Bool("SensorA")
    SensorB = Bool("SensorB")
    Alarm = Bool("Alarm")

    with Program() as logic:
        with Rung(Or(SensorA, SensorB)):
            out(Alarm)

    return logic


def _build_chain():
    """Simple 3-hop OTE chain: A → B → C → Output.

    Rung 0: A → out(B)
    Rung 1: B → out(C)
    Rung 2: C → out(Output)
    """
    A = Bool("A")
    B = Bool("B")
    C = Bool("C")
    Output = Bool("Output")

    with Program() as logic:
        with Rung(A):
            out(B)
        with Rung(B):
            out(C)
        with Rung(C):
            out(Output)

    return logic


# ---------------------------------------------------------------------------
# Stateless (OTE) tests
# ---------------------------------------------------------------------------


class TestStateless:
    """OTE-driven tags: rung condition IS the explanation."""

    def test_simple_chain_all_true(self) -> None:
        """Walk backward through a 3-hop OTE chain where everything is TRUE."""
        logic = _build_chain()
        state = SystemState().with_tags({"A": True, "B": True, "C": True, "Output": True})
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Output")

        assert result.mode == "diagnosed"
        assert result.effect.tag_name == "Output"
        assert result.effect.to_value is True

        assert len(result.conjunctive_roots) == 1
        assert result.conjunctive_roots[0].tag_name == "A"
        assert result.confidence == 1.0

        step_tags = [s.transition.tag_name for s in result.steps]
        assert "B" in step_tags
        assert "C" in step_tags
        assert "Output" in step_tags

    def test_why_not_finds_blocker(self) -> None:
        """OTE FALSE: attribute() finds the blocking contact (SERIES FALSE)."""
        logic = _build_chain()
        state = SystemState().with_tags({"A": True, "B": False, "C": False, "Output": False})
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Output")

        assert result.mode == "diagnosed"
        assert result.effect.to_value is False

        root_tags = [r.tag_name for r in result.conjunctive_roots]
        assert "A" in root_tags

    def test_or_only_true_branch_matters(self) -> None:
        """PARALLEL TRUE: only the TRUE branch is reported as root."""
        logic = _build_or_example()
        state = SystemState().with_tags({"SensorA": True, "SensorB": False, "Alarm": True})
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Alarm")

        root_tags = [r.tag_name for r in result.conjunctive_roots]
        assert "SensorA" in root_tags
        assert "SensorB" not in root_tags
        assert result.confidence == 1.0


# ---------------------------------------------------------------------------
# Stateful (latch/reset) tests
# ---------------------------------------------------------------------------


class TestStateful:
    """Latched tags: trigger may have cleared."""

    def test_latch_trigger_cleared(self) -> None:
        """Latch ON but trigger rung FALSE — contacts are ambiguous candidates."""
        logic = _build_worked_example()
        state = SystemState().with_tags(
            {
                "Sensor_Pressure": False,
                "Permissive_OK": True,
                "Faulted": False,
                "Sts_FaultTripped": True,
                "Cmd_Reset": False,
                "Alarm_Horn": True,
                "Cmd_Run": False,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Sts_FaultTripped")

        assert result.mode == "diagnosed"
        assert result.effect.to_value is True

        ambiguous_tags = [r.tag_name for r in result.ambiguous_roots]
        assert "Sensor_Pressure" in ambiguous_tags

    def test_latch_trigger_still_active(self) -> None:
        """Latch rung still TRUE — attribution is definitive, not ambiguous."""
        logic = _build_worked_example()
        state = SystemState().with_tags(
            {
                "Sensor_Pressure": True,
                "Permissive_OK": True,
                "Faulted": False,
                "Sts_FaultTripped": True,
                "Cmd_Reset": False,
                "Alarm_Horn": True,
                "Cmd_Run": False,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Sts_FaultTripped")

        assert result.mode == "diagnosed"
        conjunctive_tags = [r.tag_name for r in result.conjunctive_roots]
        assert "Sensor_Pressure" in conjunctive_tags
        assert result.confidence == 1.0

    def test_reset_path_explains_why_latch_held(self) -> None:
        """Reset rung is FALSE — the diagnosis explains what's blocking reset."""
        logic = _build_worked_example()
        state = SystemState().with_tags(
            {
                "Sensor_Pressure": False,
                "Permissive_OK": True,
                "Faulted": False,
                "Sts_FaultTripped": True,
                "Cmd_Reset": False,
                "Alarm_Horn": True,
                "Cmd_Run": False,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Sts_FaultTripped")

        reset_steps = [
            s
            for s in result.steps
            if s.rung_index == 1 and s.transition.tag_name == "Sts_FaultTripped"
        ]
        assert len(reset_steps) == 1
        reset_step = reset_steps[0]
        blocker_tags = [t.tag_name for t in reset_step.triggers]
        assert "Cmd_Reset" in blocker_tags


# ---------------------------------------------------------------------------
# Multi-tag tests
# ---------------------------------------------------------------------------


class TestMultiTag:
    """Multiple tags share the backward walk via visited set."""

    def test_multi_tag_shared_walk(self) -> None:
        """Two tags from the same chain share structure."""
        logic = _build_worked_example()
        state = SystemState().with_tags(
            {
                "Sensor_Pressure": True,
                "Permissive_OK": True,
                "Faulted": False,
                "Sts_FaultTripped": True,
                "Cmd_Reset": False,
                "Alarm_Horn": True,
                "Cmd_Run": False,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Alarm_Horn", "Sts_FaultTripped")

        assert result.mode == "diagnosed"
        assert result.effect.tag_name == "Alarm_Horn"
        assert len(result.effects) == 2
        assert result.effects[0].tag_name == "Alarm_Horn"
        assert result.effects[1].tag_name == "Sts_FaultTripped"

    def test_multi_tag_merges_at_shared_root(self) -> None:
        """Both tags ultimately trace to the same external input."""
        logic = _build_worked_example()
        state = SystemState().with_tags(
            {
                "Sensor_Pressure": True,
                "Permissive_OK": True,
                "Faulted": False,
                "Sts_FaultTripped": True,
                "Cmd_Reset": False,
                "Alarm_Horn": True,
                "Cmd_Run": False,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Alarm_Horn", "Sts_FaultTripped")

        root_tags = [r.tag_name for r in result.conjunctive_roots]
        assert root_tags.count("Sensor_Pressure") == 1


# ---------------------------------------------------------------------------
# Structural fidelity
# ---------------------------------------------------------------------------


class TestFidelity:
    """All diagnosed steps use structural fidelity."""

    def test_all_steps_structural(self) -> None:
        logic = _build_chain()
        state = SystemState().with_tags({"A": True, "B": True, "C": True, "Output": True})
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Output")

        for step in result.steps:
            assert step.fidelity == "structural"
            assert step.enablers == ()


# ---------------------------------------------------------------------------
# Fill station snapshot
# ---------------------------------------------------------------------------


class TestFillStation:
    """Diagnose the fill station example from a fault snapshot."""

    @staticmethod
    def _build():
        FillEnable = Bool("FillEnable")
        FillValve = Bool("FillValve")
        Bool("FlowSensor", external=True)
        FlowAlarm = Bool("FlowAlarm")
        StartBtn = Bool("StartBtn")
        LevelSensor = Bool("LevelSensor", external=True)

        with Program() as logic:
            with Rung(StartBtn, ~LevelSensor, ~FlowAlarm):
                latch(FillEnable)
            with Rung(LevelSensor):
                reset(FillEnable)
            with Rung(FlowAlarm):
                reset(FillEnable)
            with Rung(FillEnable):
                out(FillValve)

        return logic

    def test_fill_valve_on_traces_to_inputs(self) -> None:
        """FillValve ON → FillEnable → StartBtn + ~LevelSensor + ~FlowAlarm."""
        logic = self._build()
        state = SystemState().with_tags(
            {
                "StartBtn": True,
                "LevelSensor": False,
                "FlowAlarm": False,
                "FillEnable": True,
                "FillValve": True,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("FillValve")

        assert result.mode == "diagnosed"
        root_tags = [r.tag_name for r in result.conjunctive_roots]
        assert "StartBtn" in root_tags

    def test_fill_valve_off_finds_blocker(self) -> None:
        """FillValve OFF because FillEnable is OFF — reset by FlowAlarm."""
        logic = self._build()
        state = SystemState().with_tags(
            {
                "StartBtn": True,
                "LevelSensor": False,
                "FlowAlarm": True,
                "FillEnable": False,
                "FillValve": False,
            }
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("FillValve")

        assert result.mode == "diagnosed"
        assert result.effect.to_value is False


# ---------------------------------------------------------------------------
# Phase 2: Write-before-read skipping
# ---------------------------------------------------------------------------


class TestWriteBeforeRead:
    """Tags unconditionally written before read are scan-local noise."""

    @staticmethod
    def _build():
        """Program where Intermediate is always written before read.

        Rung 0: (unconditional) copy(Sensor, Intermediate)
        Rung 1: Intermediate → out(Output)
        """
        Sensor = Bool("Sensor")
        Intermediate = Bool("Intermediate")
        Output = Bool("Output")

        with Program() as logic:
            with Rung():
                copy(Sensor, Intermediate)
            with Rung(Intermediate):
                out(Output)

        return logic

    def test_wbr_tag_skipped(self) -> None:
        """Intermediate is write-before-read — walk should skip it."""
        logic = self._build()
        state = SystemState().with_tags({"Sensor": True, "Intermediate": True, "Output": True})
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Output")

        step_tags = [s.transition.tag_name for s in result.steps]
        assert "Intermediate" not in step_tags

        root_tags = [r.tag_name for r in result.conjunctive_roots]
        assert "Intermediate" not in root_tags


# ---------------------------------------------------------------------------
# Phase 2: Init-constant pinning
# ---------------------------------------------------------------------------


class TestInitConstant:
    """Init-constant tags are evidence anchors — treated as leaves."""

    @staticmethod
    def _build():
        """Program with a latch-guarded init constant.

        Rung 0: ~InitDone → copy(42, Setpoint), latch(InitDone)
        Rung 1: And(Enable, Sensor) → out(Output)

        InitDone is a self-latching Bool guard (Pattern A).
        Setpoint is written only under that guard with a literal value.
        """
        InitDone = Bool("InitDone")
        Setpoint = Int("Setpoint")
        Enable = Bool("Enable")
        Sensor = Bool("Sensor")
        Output = Bool("Output")

        with Program() as logic:
            with Rung(~InitDone):
                copy(42, Setpoint)
                latch(InitDone)
            with Rung(And(Enable, Sensor)):
                out(Output)

        return logic

    def test_init_constant_is_leaf(self) -> None:
        """InitDone (self-latching guard) should be treated as a leaf."""
        logic = self._build()
        state = SystemState().with_tags(
            {"InitDone": True, "Setpoint": 42, "Enable": True, "Sensor": True, "Output": True}
        )
        plc = PLC(logic=logic, initial_state=state)
        result = plc.diagnose("Output")

        step_tags = [s.transition.tag_name for s in result.steps]
        assert "InitDone" not in step_tags


# ---------------------------------------------------------------------------
# Phase 2: Back-propagation
# ---------------------------------------------------------------------------


class TestBackPropagation:
    """Functional dependencies constrain source values from targets."""

    @staticmethod
    def _build():
        """Program with a calc chain: Scaled = Raw + 10.

        Rung 0: (unconditional) calc(Raw + 10, Scaled)
        Rung 1: Enable → out(Output)
        """
        Raw = Int("Raw")
        Scaled = Int("Scaled")
        Enable = Bool("Enable")
        Output = Bool("Output")

        with Program() as logic:
            with Rung():
                calc(Raw + 10, Scaled)
            with Rung(Enable):
                out(Output)

        return logic

    def test_back_propagation_infers_source(self) -> None:
        """back_propagate_value should infer Raw from Scaled."""
        from pyrung.core.analysis.reverse_edges import (
            back_propagate_value,
            build_reverse_edge_map,
        )

        logic = self._build()
        edge_map = build_reverse_edge_map(logic)
        result = back_propagate_value(edge_map, "Scaled", 42)

        assert "Raw" in result
        assert result["Raw"] == 32

    def test_back_propagation_identity_copy(self) -> None:
        """Identity copy: back-propagation through copy(A, B)."""
        from pyrung.core.analysis.reverse_edges import (
            back_propagate_value,
            build_reverse_edge_map,
        )

        A = Int("A")
        B = Int("B")

        with Program() as logic:
            with Rung():
                copy(A, B)

        edge_map = build_reverse_edge_map(logic)
        result = back_propagate_value(edge_map, "B", 99)
        assert result.get("A") == 99
