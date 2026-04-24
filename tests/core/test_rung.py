"""Tests for Rung class.

Rungs evaluate conditions and execute instructions.
They are pure functions: evaluate(state) -> new_state.
"""

import pytest

from pyrung.core import Bool, Int, Program, Rung, SystemState
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

    def test_rung_with_int_truthiness_condition_nonzero(self):
        """INT tags in rung conditions evaluate true when nonzero."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Light = Bool("Light")

        rung = Rung(Step)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Step": 1, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_int_truthiness_condition_zero(self):
        """INT tags in rung conditions evaluate false when zero."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Light = Bool("Light")

        rung = Rung(Step)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Step": 0, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is False

    def test_rung_with_int_truthiness_condition_negative(self):
        """INT tags in rung conditions evaluate true when negative."""
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Light = Bool("Light")

        rung = Rung(Step)
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Step": -1, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_dint_direct_condition_rejected(self):
        """Only BOOL and INT tags can be direct rung conditions."""
        import pytest

        from pyrung.core import Dint
        from pyrung.core.rung import Rung

        Step32 = Dint("Step32")

        with pytest.raises(TypeError, match="BOOL and INT"):
            Rung(Step32)

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
        rung.add_instruction(CopyInstruction(source=1, dest=Step))
        rung.add_instruction(CopyInstruction(source=2, dest=Step))

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
    """Test rung with Or() composite condition (OR logic)."""

    def test_rung_with_any_of_first_true(self):
        """Rung executes when first condition in Or is true."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(Or(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Start": True, "CmdStart": False, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_any_of_second_true(self):
        """Rung executes when second condition in Or is true."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(Or(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Start": False, "CmdStart": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_any_of_all_false(self):
        """Rung does not execute when all conditions in Or are false."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(Or(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Start": False, "CmdStart": False, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is False

    def test_rung_with_any_of_int_truthiness(self):
        """Or accepts INT tags and treats nonzero as true."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(Or(Step, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Step": 2, "CmdStart": False, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        state = SystemState().with_tags({"Step": 0, "CmdStart": False, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False

    def test_rung_with_and_plus_any_of(self):
        """Rung with AND condition plus Or (Step == 1, Or(Start, CmdStart))."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        # Step == 1 AND (Start OR CmdStart)
        rung = Rung(Step == 1, Or(Start, CmdStart))
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


class TestRungWithOr:
    """Test rung with Or() for OR conditions."""

    def test_rung_with_or(self):
        """Rung executes when Or() condition is true."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        rung = Rung(Or(Start, CmdStart))
        rung.add_instruction(OutInstruction(Light))

        # Only CmdStart is true
        state = SystemState().with_tags({"Start": False, "CmdStart": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_three_way_or(self):
        """Rung with three-way Or(A, B, C)."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        A = Bool("A")
        B = Bool("B")
        C = Bool("C")
        Light = Bool("Light")

        rung = Rung(Or(A, B, C))
        rung.add_instruction(OutInstruction(Light))

        # Only C is true
        state = SystemState().with_tags({"A": False, "B": False, "C": True, "Light": False})
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Light"] is True

    def test_rung_with_and_plus_or(self):
        """Rung with AND condition plus Or()."""
        from pyrung.core import Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Start = Bool("Start")
        CmdStart = Bool("CmdStart")
        Light = Bool("Light")

        # Step == 1 AND (Start OR CmdStart)
        rung = Rung(Step == 1, Or(Start, CmdStart))
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


class TestRungWithAllOf:
    """Test rung with And() composite condition (AND logic)."""

    def test_rung_with_all_of(self):
        """Rung executes only when all conditions in And are true."""
        from pyrung.core import And
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Ready = Bool("Ready")
        Auto = Bool("Auto")
        Light = Bool("Light")

        rung = Rung(And(Ready, Auto))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Ready": True, "Auto": True, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        state = SystemState().with_tags({"Ready": True, "Auto": False, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False

    def test_rung_with_all_of_int_truthiness(self):
        """And accepts INT tags and treats nonzero as true."""
        from pyrung.core import And
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Step = Int("Step")
        Auto = Bool("Auto")
        Light = Bool("Light")

        rung = Rung(And(Step, Auto))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Step": 1, "Auto": True, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        state = SystemState().with_tags({"Step": 0, "Auto": True, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False


class TestRungBranchIntTruthiness:
    """Branch conditions support INT truthiness via shared normalization."""

    def test_branch_with_int_condition(self):
        from pyrung.core import Program, Rung, branch, out
        from tests.conftest import evaluate_program

        Enable = Bool("Enable")
        Step = Int("Step")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Enable):
                with branch(Step):
                    out(Light)

        state = SystemState().with_tags({"Enable": True, "Step": 1, "Light": False})
        new_state = evaluate_program(logic, state)
        assert new_state.tags["Light"] is True

        state = SystemState().with_tags({"Enable": True, "Step": 0, "Light": False})
        new_state = evaluate_program(logic, state)
        assert new_state.tags["Light"] is False


class TestRungWithGroupedAnyOf:
    """Test explicit grouped AND terms inside Or()."""

    def test_rung_with_any_of_group(self):
        """Rung executes when explicit And() group inside Or() is true."""
        from pyrung.core import And, Or
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Start = Bool("Start")
        Ready = Bool("Ready")
        Auto = Bool("Auto")
        Light = Bool("Light")

        rung = Rung(Or(Start, And(Ready, Auto)))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags(
            {"Start": False, "Ready": True, "Auto": True, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        state = SystemState().with_tags(
            {"Start": False, "Ready": True, "Auto": False, "Light": False}
        )
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False


class TestRungWithAnd:
    """Test rung with And() for AND conditions."""

    def test_rung_with_and(self):
        """Rung executes when both And() conditions are true."""
        from pyrung.core import And
        from pyrung.core.instruction import OutInstruction
        from pyrung.core.rung import Rung

        Ready = Bool("Ready")
        Auto = Bool("Auto")
        Light = Bool("Light")

        rung = Rung(And(Ready, Auto))
        rung.add_instruction(OutInstruction(Light))

        state = SystemState().with_tags({"Ready": True, "Auto": True, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is True

        state = SystemState().with_tags({"Ready": True, "Auto": False, "Light": False})
        new_state = evaluate_rung(rung, state)
        assert new_state.tags["Light"] is False


class TestRungComment:
    """Test rung comment property via context manager."""

    def test_comment_default_is_none(self):
        from pyrung.core.program import out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)

        assert logic.rungs[0].comment is None

    def test_comment_set_and_get(self):
        from pyrung.core.program import comment, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            comment("Initialize the light system.")
            with Rung(Button):
                out(Light)

        assert logic.rungs[0].comment == "Initialize the light system."

    def test_comment_strips_whitespace(self):
        from pyrung.core.program import comment, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            comment("  padded text  ")
            with Rung(Button):
                out(Light)

        assert logic.rungs[0].comment == "padded text"

    def test_comment_dedents_triple_quoted(self):
        from pyrung.core.program import comment, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            comment("""
                    Line one.
                    Line two.
                """)
            with Rung(Button):
                out(Light)

        assert logic.rungs[0].comment == "Line one.\nLine two."

    def test_comment_exceeding_max_length_raises(self):
        from pyrung.core.program import comment

        with Program():
            with pytest.raises(ValueError, match="1400"):
                comment("x" * 1401)

    def test_comment_at_max_length_is_accepted(self):
        from pyrung.core.program import comment, out

        Button = Bool("Button")
        Light = Bool("Light")

        with Program() as logic:
            comment("x" * 1400)
            with Rung(Button):
                out(Light)

        assert logic.rungs[0].comment is not None
        assert len(logic.rungs[0].comment) == 1400

    def test_comment_double_call_raises(self):
        from pyrung.core.program import comment

        with Program():
            comment("first")
            with pytest.raises(RuntimeError, match="already set"):
                comment("second")

    def test_comment_outside_program_raises(self):
        from pyrung.core.program import comment

        with pytest.raises(RuntimeError, match="Program context"):
            comment("orphan")
