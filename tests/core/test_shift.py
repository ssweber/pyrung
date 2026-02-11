"""Tests for shift instruction and chaining API."""

import pytest

from pyrung.core import Block, Bool, Int, PLCRunner, Program, Rung, SystemState, TagType, shift
from tests.conftest import evaluate_program


class TestShiftInstruction:
    """Behavior tests for shift register runtime semantics."""

    def test_rising_edge_only(self):
        """Shift occurs only on clock OFF->ON transitions."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(1, 3)).clock(Clock).reset(Reset)

        runner = PLCRunner(logic)
        runner.patch(
            {"Data": True, "Clock": False, "Reset": False, "C1": True, "C2": False, "C3": False}
        )
        runner.step()
        assert runner.current_state.tags["C1"] is True
        assert runner.current_state.tags["C2"] is False
        assert runner.current_state.tags["C3"] is False

        # Rising edge: shift once.
        runner.patch({"Clock": True})
        runner.step()
        assert runner.current_state.tags["C1"] is True
        assert runner.current_state.tags["C2"] is True
        assert runner.current_state.tags["C3"] is False

        # Still high: no additional shift.
        runner.step()
        assert runner.current_state.tags["C1"] is True
        assert runner.current_state.tags["C2"] is True
        assert runner.current_state.tags["C3"] is False

        # Falling edge: no shift.
        runner.patch({"Clock": False})
        runner.step()
        assert runner.current_state.tags["C1"] is True
        assert runner.current_state.tags["C2"] is True
        assert runner.current_state.tags["C3"] is False

    def test_reset_clears_full_range(self):
        """Reset condition clears every bit in the selected range."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(2, 7)).clock(Clock).reset(Reset)

        state = SystemState().with_tags(
            {
                "Data": True,
                "Clock": False,
                "Reset": True,
                "C2": True,
                "C3": True,
                "C4": True,
                "C5": True,
                "C6": True,
                "C7": True,
            }
        )
        new_state = evaluate_program(logic, state)
        for name in ("C2", "C3", "C4", "C5", "C6", "C7"):
            assert new_state.tags[name] is False

    def test_reset_overwrites_on_simultaneous_clock_edge(self):
        """Reset dominates outputs when reset and clock edge happen together."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(1, 3)).clock(Clock).reset(Reset)

        runner = PLCRunner(logic)
        runner.patch(
            {"Data": True, "Clock": False, "Reset": False, "C1": False, "C2": True, "C3": True}
        )
        runner.step()

        # Rising edge with reset active.
        runner.patch({"Clock": True, "Reset": True})
        runner.step()
        assert runner.current_state.tags["C1"] is False
        assert runner.current_state.tags["C2"] is False
        assert runner.current_state.tags["C3"] is False

    def test_direction_forward_low_to_high(self):
        """shift(C.select(...)) moves data from lower addresses to higher addresses."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(2, 7)).clock(Clock).reset(Reset)

        runner = PLCRunner(logic)
        runner.patch({"Data": False, "Clock": False, "Reset": False, "C2": True, "C7": False})
        runner.step()
        runner.patch({"Clock": True})
        runner.step()

        assert runner.current_state.tags["C2"] is False
        assert runner.current_state.tags["C3"] is True

    def test_direction_reverse_high_to_low(self):
        """shift(C.select(...).reverse()) moves data from higher to lower addresses."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(2, 7).reverse()).clock(Clock).reset(Reset)

        runner = PLCRunner(logic)
        runner.patch({"Data": False, "Clock": False, "Reset": False, "C2": False, "C7": True})
        runner.step()
        runner.patch({"Clock": True})
        runner.step()

        assert runner.current_state.tags["C7"] is False
        assert runner.current_state.tags["C6"] is True

    def test_rung_true_shifts_in_true(self):
        """When rung condition is true, shifted-in data bit is true."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(1, 3)).clock(Clock).reset(Reset)

        runner = PLCRunner(logic)
        runner.patch(
            {"Data": True, "Clock": False, "Reset": False, "C1": False, "C2": False, "C3": False}
        )
        runner.step()
        runner.patch({"Clock": True})
        runner.step()

        assert runner.current_state.tags["C1"] is True

    def test_rung_false_shifts_in_false(self):
        """Terminal execution still shifts on clock edge even when rung is false."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(1, 3)).clock(Clock).reset(Reset)

        runner = PLCRunner(logic)
        runner.patch(
            {"Data": False, "Clock": False, "Reset": False, "C1": True, "C2": True, "C3": False}
        )
        runner.step()
        runner.patch({"Clock": True})
        runner.step()

        assert runner.current_state.tags["C1"] is False
        assert runner.current_state.tags["C2"] is True
        assert runner.current_state.tags["C3"] is True

    def test_non_bool_range_raises(self):
        """Shift requires a BOOL range."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        DS = Block("DS", TagType.INT, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(DS.select(1, 3)).clock(Clock).reset(Reset)

        state = SystemState().with_tags({"Data": True, "Clock": True, "Reset": False})
        with pytest.raises(TypeError, match="BOOL"):
            evaluate_program(logic, state)

    def test_indirect_reversed_bounds_raises(self):
        """Indirect resolved range with start > end is invalid."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        Start = Int("Start")
        End = Int("End")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(Start, End)).clock(Clock).reset(Reset)

        state = SystemState().with_tags(
            {"Data": True, "Clock": False, "Reset": False, "Start": 8, "End": 2}
        )
        with pytest.raises(ValueError, match="must be <="):
            evaluate_program(logic, state)

    def test_indirect_empty_resolved_range_raises(self):
        """Indirect resolved sparse range with no valid addresses is invalid."""
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        Start = Int("Start")
        End = Int("End")
        X = Block("X", TagType.BOOL, 1, 816, valid_ranges=((1, 16), (21, 36)))

        with Program() as logic:
            with Rung(Data):
                shift(X.select(Start, End)).clock(Clock).reset(Reset)

        state = SystemState().with_tags(
            {"Data": True, "Clock": False, "Reset": False, "Start": 17, "End": 20}
        )
        with pytest.raises(ValueError, match="empty range"):
            evaluate_program(logic, state)


class TestShiftBuilder:
    """Builder API tests for shift().clock().reset()."""

    def test_clock_then_reset_adds_and_executes(self):
        Data = Bool("Data")
        Clock = Bool("Clock")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(1, 3)).clock(Clock).reset(Reset)

        state = SystemState().with_tags({"Data": True, "Clock": True, "Reset": False, "C1": False})
        new_state = evaluate_program(logic, state)
        assert new_state.tags["C1"] is True

    def test_reset_before_clock_raises(self):
        Data = Bool("Data")
        Reset = Bool("Reset")
        C = Block("C", TagType.BOOL, 1, 100)

        with pytest.raises(RuntimeError, match="clock"):
            with Program():
                with Rung(Data):
                    shift(C.select(1, 3)).reset(Reset)

    def test_clock_without_final_reset_adds_nothing(self):
        Data = Bool("Data")
        Clock = Bool("Clock")
        C = Block("C", TagType.BOOL, 1, 100)

        with Program() as logic:
            with Rung(Data):
                shift(C.select(1, 3)).clock(Clock)

        runner = PLCRunner(logic)
        runner.patch({"Data": True, "Clock": False, "C1": True, "C2": False, "C3": False})
        runner.step()
        runner.patch({"Clock": True})
        runner.step()

        assert runner.current_state.tags["C1"] is True
        assert runner.current_state.tags["C2"] is False
        assert runner.current_state.tags["C3"] is False
