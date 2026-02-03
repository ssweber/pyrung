"""Tests for Instruction classes.

Instructions are pure functions that transform SystemState.
They return a new state, never mutating the input.
"""

from pyrung.core import Bit, Int, SystemState
from tests.conftest import execute


class TestOutInstruction:
    """Test OUT instruction (output coil)."""

    def test_out_sets_bit_true(self):
        """OUT sets the target bit to True."""
        from pyrung.core.instruction import OutInstruction

        Light = Bit("Light")
        instr = OutInstruction(Light)

        state = SystemState().with_tags({"Light": False})
        new_state = execute(instr, state)

        assert new_state.tags["Light"] is True
        assert state.tags["Light"] is False  # Original unchanged

    def test_out_creates_tag_if_missing(self):
        """OUT creates the tag if it doesn't exist."""
        from pyrung.core.instruction import OutInstruction

        Light = Bit("Light")
        instr = OutInstruction(Light)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Light"] is True


class TestLatchInstruction:
    """Test LATCH/SET instruction."""

    def test_latch_sets_bit_true(self):
        """LATCH sets the target bit to True."""
        from pyrung.core.instruction import LatchInstruction

        Motor = Bit("Motor")
        instr = LatchInstruction(Motor)

        state = SystemState().with_tags({"Motor": False})
        new_state = execute(instr, state)

        assert new_state.tags["Motor"] is True


class TestResetInstruction:
    """Test RESET/UNLATCH instruction."""

    def test_reset_sets_bit_false(self):
        """RESET sets the target bit to False."""
        from pyrung.core.instruction import ResetInstruction

        Motor = Bit("Motor")
        instr = ResetInstruction(Motor)

        state = SystemState().with_tags({"Motor": True})
        new_state = execute(instr, state)

        assert new_state.tags["Motor"] is False

    def test_reset_uses_tag_default(self):
        """RESET sets to tag's default value (0 for int)."""
        from pyrung.core.instruction import ResetInstruction

        Counter = Int("Counter")
        instr = ResetInstruction(Counter)

        state = SystemState().with_tags({"Counter": 100})
        new_state = execute(instr, state)

        assert new_state.tags["Counter"] == 0


class TestCopyInstruction:
    """Test COPY instruction."""

    def test_copy_literal_to_tag(self):
        """COPY copies a literal value to target tag."""
        from pyrung.core.instruction import CopyInstruction

        Step = Int("Step")
        instr = CopyInstruction(source=5, target=Step)

        state = SystemState().with_tags({"Step": 0})
        new_state = execute(instr, state)

        assert new_state.tags["Step"] == 5

    def test_copy_tag_to_tag(self):
        """COPY copies value from source tag to target tag."""
        from pyrung.core.instruction import CopyInstruction

        Source = Int("Source")
        Target = Int("Target")
        instr = CopyInstruction(source=Source, target=Target)

        state = SystemState().with_tags({"Source": 42, "Target": 0})
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == 42
        assert new_state.tags["Source"] == 42  # Source unchanged


class TestOneShotBehavior:
    """Test one-shot instruction behavior."""

    def test_out_oneshot_executes_once(self):
        """One-shot OUT only executes on first call per rung activation."""
        from pyrung.core.instruction import OutInstruction

        Light = Bit("Light")
        instr = OutInstruction(Light, oneshot=True)

        state = SystemState().with_tags({"Light": False})

        # First execution - should set to True
        new_state = execute(instr, state)
        assert new_state.tags["Light"] is True

        # Second execution - should not change (already triggered)
        # Note: oneshot state is tracked in the instruction, not in SystemState
        new_state2 = execute(instr, new_state.with_tags({"Light": False}))
        assert new_state2.tags["Light"] is False  # Not set again

    def test_oneshot_resets_after_rung_false(self):
        """One-shot resets when reset_oneshot() is called."""
        from pyrung.core.instruction import OutInstruction

        Light = Bit("Light")
        instr = OutInstruction(Light, oneshot=True)

        state = SystemState().with_tags({"Light": False})

        # First execution
        new_state = execute(instr, state)
        assert new_state.tags["Light"] is True

        # Reset oneshot (simulating rung going false)
        instr.reset_oneshot()

        # Now it should execute again
        new_state2 = execute(instr, new_state.with_tags({"Light": False}))
        assert new_state2.tags["Light"] is True


class TestInstructionImmutability:
    """Test that instructions don't mutate input state."""

    def test_out_does_not_mutate_input(self):
        """OUT instruction returns new state, input unchanged."""
        from pyrung.core.instruction import OutInstruction

        Light = Bit("Light")
        instr = OutInstruction(Light)

        original_state = SystemState().with_tags({"Light": False})
        new_state = execute(instr, original_state)

        # Original should be unchanged
        assert original_state.tags["Light"] is False
        assert new_state.tags["Light"] is True
        assert original_state is not new_state

    def test_copy_does_not_mutate_input(self):
        """COPY instruction returns new state, input unchanged."""
        from pyrung.core.instruction import CopyInstruction

        Step = Int("Step")
        instr = CopyInstruction(source=99, target=Step)

        original_state = SystemState().with_tags({"Step": 0})
        new_state = execute(instr, original_state)

        assert original_state.tags["Step"] == 0
        assert new_state.tags["Step"] == 99
