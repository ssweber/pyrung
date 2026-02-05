"""Tests for Instruction classes.

Instructions are pure functions that transform SystemState.
They return a new state, never mutating the input.
"""

import pytest

from pyrung.core import Bit, Block, Int, SystemState, TagType
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


class TestBlockCopyInstruction:
    """Test BLOCKCOPY instruction."""

    def test_blockcopy_copies_range(self):
        """BLOCKCOPY copies all values from source range to dest range."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        source = DS.select(1, 3)
        dest = DD.select(10, 12)
        instr = BlockCopyInstruction(source, dest)

        state = SystemState().with_tags({"DS1": 10, "DS2": 20, "DS3": 30})
        new_state = execute(instr, state)

        assert new_state.tags["DD10"] == 10
        assert new_state.tags["DD11"] == 20
        assert new_state.tags["DD12"] == 30

    def test_blockcopy_does_not_mutate_source(self):
        """BLOCKCOPY does not alter source values."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        instr = BlockCopyInstruction(DS.select(1, 2), DD.select(1, 2))

        state = SystemState().with_tags({"DS1": 42, "DS2": 99, "DD1": 0, "DD2": 0})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 42
        assert new_state.tags["DS2"] == 99
        assert new_state.tags["DD1"] == 42
        assert new_state.tags["DD2"] == 99

    def test_blockcopy_length_mismatch_raises(self):
        """BLOCKCOPY raises ValueError when ranges have different lengths."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        instr = BlockCopyInstruction(DS.select(1, 3), DD.select(1, 2))

        state = SystemState().with_tags({"DS1": 1, "DS2": 2, "DS3": 3})
        with pytest.raises(ValueError, match="length mismatch"):
            execute(instr, state)

    def test_blockcopy_single_element(self):
        """BLOCKCOPY works with single-element ranges."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        instr = BlockCopyInstruction(DS.select(5, 5), DD.select(1, 1))

        state = SystemState().with_tags({"DS5": 777})
        new_state = execute(instr, state)

        assert new_state.tags["DD1"] == 777

    def test_blockcopy_oneshot(self):
        """BLOCKCOPY with oneshot only executes once."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        instr = BlockCopyInstruction(DS.select(1, 2), DD.select(1, 2), oneshot=True)

        state = SystemState().with_tags({"DS1": 10, "DS2": 20, "DD1": 0, "DD2": 0})

        # First execution copies
        new_state = execute(instr, state)
        assert new_state.tags["DD1"] == 10
        assert new_state.tags["DD2"] == 20

        # Second execution does not copy (oneshot already fired)
        state2 = new_state.with_tags({"DS1": 99, "DS2": 99})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["DD1"] == 10  # Unchanged
        assert new_state2.tags["DD2"] == 20

    def test_blockcopy_uses_defaults_for_missing_source(self):
        """BLOCKCOPY uses tag defaults when source tags have no value."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        DD = Block("DD", TagType.DINT, 1, 100)

        instr = BlockCopyInstruction(DS.select(1, 2), DD.select(1, 2))

        state = SystemState()  # No tags set
        new_state = execute(instr, state)

        assert new_state.tags["DD1"] == 0  # INT default
        assert new_state.tags["DD2"] == 0

    def test_blockcopy_same_block(self):
        """BLOCKCOPY works within the same block (non-overlapping)."""
        from pyrung.core.instruction import BlockCopyInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = BlockCopyInstruction(DS.select(1, 3), DS.select(10, 12))

        state = SystemState().with_tags({"DS1": 100, "DS2": 200, "DS3": 300})
        new_state = execute(instr, state)

        assert new_state.tags["DS10"] == 100
        assert new_state.tags["DS11"] == 200
        assert new_state.tags["DS12"] == 300


class TestFillInstruction:
    """Test FILL instruction."""

    def test_fill_writes_constant_to_range(self):
        """FILL writes the same value to every element."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(0, DS.select(1, 5))

        state = SystemState().with_tags({"DS1": 10, "DS2": 20, "DS3": 30, "DS4": 40, "DS5": 50})
        new_state = execute(instr, state)

        for i in range(1, 6):
            assert new_state.tags[f"DS{i}"] == 0

    def test_fill_with_nonzero_value(self):
        """FILL writes a non-zero constant."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(999, DS.select(10, 12))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DS10"] == 999
        assert new_state.tags["DS11"] == 999
        assert new_state.tags["DS12"] == 999

    def test_fill_with_tag_value(self):
        """FILL resolves source Tag value and writes to all elements."""
        from pyrung.core.instruction import FillInstruction

        FillVal = Int("FillVal")
        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(FillVal, DS.select(1, 3))

        state = SystemState().with_tags({"FillVal": 42})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 42
        assert new_state.tags["DS2"] == 42
        assert new_state.tags["DS3"] == 42

    def test_fill_oneshot(self):
        """FILL with oneshot only executes once."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(0, DS.select(1, 2), oneshot=True)

        state = SystemState().with_tags({"DS1": 10, "DS2": 20})

        # First execution clears
        new_state = execute(instr, state)
        assert new_state.tags["DS1"] == 0
        assert new_state.tags["DS2"] == 0

        # Second execution does nothing (oneshot already fired)
        state2 = new_state.with_tags({"DS1": 99, "DS2": 99})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["DS1"] == 99  # Unchanged
        assert new_state2.tags["DS2"] == 99

    def test_fill_single_element(self):
        """FILL works with single-element ranges."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(123, DS.select(50, 50))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DS50"] == 123

    def test_fill_does_not_mutate_input(self):
        """FILL returns new state, input unchanged."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(0, DS.select(1, 2))

        original = SystemState().with_tags({"DS1": 10, "DS2": 20})
        new_state = execute(instr, original)

        assert original.tags["DS1"] == 10
        assert original.tags["DS2"] == 20
        assert new_state.tags["DS1"] == 0
        assert new_state.tags["DS2"] == 0
