"""Tests for Rung class.

Rungs evaluate conditions and execute instructions.
They are pure functions: evaluate(state) -> new_state.
"""

from pyrung.core import Bool, Int, SystemState
from tests.conftest import evaluate_rung


class TestRungConditions:
    """Test rung condition evaluation."""

    def test_rung_with_true_bit_condition(self):
        """Rung executes when bit condition is true."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Button = Bool("Button")
        Light = Bool("Light")

        rung = Rung(Button)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Button": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_false_bit_condition(self):
        """Rung does not execute when bit condition is false."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Button = Bool("Button")
        Light = Bool("Light")

        rung = Rung(Button)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Button": False, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is False

    def test_rung_with_comparison_condition(self):
        """Rung executes when comparison condition is true."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Light = Bool("Light")

        rung = Rung(Step == 0)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Step": 0, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_multiple_conditions_all_true(self):
        """Rung executes when ALL conditions are true (AND logic)."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Button1 = Bool("Button1")
        Button2 = Bool("Button2")
        Light = Bool("Light")

        rung = Rung(Button1, Button2)  # Both must be true
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Button1": True, "Button2": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_multiple_conditions_one_false(self):
        """Rung does not execute when any condition is false."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Button1 = Bool("Button1")
        Button2 = Bool("Button2")
        Light = Bool("Light")

        rung = Rung(Button1, Button2)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Button1": True, "Button2": False, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is False

    def test_rung_unconditional(self):
        """Rung with no conditions always executes."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Light = Bool("Light")

        rung = Rung()  # No conditions
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True


class TestRungInstructions:
    """Test rung instruction execution."""

    def test_multiple_instructions_execute_in_order(self):
        """Multiple instructions execute sequentially."""
        from pyrung.core.instruction import CopyInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")

        rung = Rung()  # Unconditional
        rung.add_instruction(CopyInstruction(source=1, target=Step))
        rung.add_instruction(CopyInstruction(source=2, target=Step))

        state = SystemState().with_tags({"Step": 0})
        new_state = evaluate_rung(rung, state)

        # Second instruction overwrites first
        assert new_state.tags["Step"] == 2


class TestRungOutputHandling:
    """Test how rungs handle outputs when conditions go false."""

    def test_out_resets_when_rung_false(self):
        """OUT coils reset to False when rung goes false."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Button = Bool("Button")
        Light = Bool("Light")

        rung = Rung(Button)
        rung.add_instruction(OutInstruction(Light))
        rung.register_coil(Light)  # Mark as coil output

        # First, rung is true - Light turns on
        state = SystemState().with_tags({"Button": True, "Light": False})
        state = evaluate_rung(rung, state)
        assert state.tags["Light"] is True

        # Then, rung is false - Light should turn off
        state = state.with_tags({"Button": False})
        state = evaluate_rung(rung, state)
        assert state.tags["Light"] is False

    def test_latch_not_reset_when_rung_false(self):
        """LATCH outputs are NOT reset when rung goes false."""
        from pyrung.core.instruction import LatchInstruction
        from pyrung.core.rung import Rung

        Button = Bool("Button")
        Motor = Bool("Motor")

        rung = Rung(Button)
        rung.add_instruction(LatchInstruction(Motor))
        # Note: NOT registered as coil - latches are not auto-reset

        # First, rung is true - Motor latches on
        state = SystemState().with_tags({"Button": True, "Motor": False})
        state = evaluate_rung(rung, state)
        assert state.tags["Motor"] is True

        # Then, rung is false - Motor stays on
        state = state.with_tags({"Button": False})
        state = evaluate_rung(rung, state)
        assert state.tags["Motor"] is True  # Still on!


class TestRungImmutability:
    """Test that rung evaluation doesn't mutate input state."""

    def test_evaluate_returns_new_state(self):
        """Rung.evaluate() returns new state, input unchanged."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Button = Bool("Button")
        Light = Bool("Light")

        rung = Rung(Button)
        rung.add_instruction(OutInstruction(Light))

        original = SystemState().with_tags({"Button": True, "Light": False})
        result = evaluate_rung(rung, original)

        assert original.tags["Light"] is False
        assert result.tags["Light"] is True
        assert original is not result


class TestRungWithAnyOf:
    """Test rung with any_of() composite condition (OR logic)."""

    def test_rung_with_any_of_first_true(self):
        """Rung executes when first condition in any_of is true."""
        from pyrung.core import any_of
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(any_of(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Start": True, "CmdStart": False, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_any_of_second_true(self):
        """Rung executes when second condition in any_of is true."""
        from pyrung.core import any_of
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(any_of(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Start": False, "CmdStart": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_any_of_all_false(self):
        """Rung does not execute when all conditions in any_of are false."""
        from pyrung.core import any_of
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(any_of(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Start": False, "CmdStart": False, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is False

    def test_rung_with_and_plus_any_of(self):
        """Rung with AND condition plus any_of (Step == 1, any_of(Start, CmdStart))."""
        from pyrung.core import any_of
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        # Step == 1 AND (Start OR CmdStart)
        rung = Rung(Step == 1, any_of(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        # Step is 1, Start is true -> executes
        state = SystemState().with_tags(
            {"Step": 1, "Start": True, "CmdStart": False, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        # Step is 1, CmdStart is true -> executes
        state = SystemState().with_tags(
            {"Step": 1, "Start": False, "CmdStart": True, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        # Step is 0, both triggers true -> does NOT execute (AND fails)
        state = SystemState().with_tags(
            {"Step": 0, "Start": True, "CmdStart": True, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False

        # Step is 1, both triggers false -> does NOT execute (OR fails)
        state = SystemState().with_tags(
            {"Step": 1, "Start": False, "CmdStart": False, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False


class TestRungWithBitwiseOr:
    """Test rung with | operator for OR conditions."""

    def test_rung_with_pipe_operator(self):
        """Rung executes when | condition is true."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(Start | CmdStart)
        rung.add_instruction(OutInstruction(Light))

        # Only CmdStart is true
        state = SystemState().with_tags({"Start": False, "CmdStart": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_chained_pipe(self):
        """Rung with chained | operators (A | B | C)."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Light = Bool("Light")

        rung = Rung(A | B | C)
        rung.add_instruction(OutInstruction(Light))

        # Only C is true
        state = SystemState().with_tags({"A": False, "B": False, "C": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_and_plus_pipe(self):
        """Rung with AND condition plus | operator."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        # Step == 1 AND (Start OR CmdStart)
        rung = Rung(Step == 1, Start | CmdStart)
        rung.add_instruction(OutInstruction(Light))

        # Step is 1, CmdStart is true -> executes
        state = SystemState().with_tags(
            {"Step": 1, "Start": False, "CmdStart": True, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        # Step is 0 -> does NOT execute
        state = SystemState().with_tags(
            {"Step": 0, "Start": True, "CmdStart": True, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False
