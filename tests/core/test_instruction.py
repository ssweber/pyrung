"""Tests for Instruction classes.

Instructions are pure functions that transform SystemState.
They return a new state, never mutating the input.
"""

import struct

import pytest

from pyrung.core import Block, Bool, Dint, Int, Real, SystemState, TagType, Word
from tests.conftest import evaluate_rung, execute


class TestOutInstruction:
    """Test OUT instruction (output coil)."""

    def test_out_sets_bit_true(self):
        """OUT sets the target bit to True."""
        from pyrung.core.instruction import OutInstruction

        Light = Bool("Light")
        instr = OutInstruction(Light)

        state = SystemState().with_tags({"Light": False})
        new_state = execute(instr, state)

        assert new_state.tags["Light"] is True
        assert state.tags["Light"] is False  # Original unchanged

    def test_out_creates_tag_if_missing(self):
        """OUT creates the tag if it doesn't exist."""
        from pyrung.core.instruction import OutInstruction

        Light = Bool("Light")
        instr = OutInstruction(Light)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Light"] is True

    def test_out_sets_block_range_true(self):
        """OUT sets all bits in a selected range to True."""
        from pyrung.core.instruction import OutInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        instr = OutInstruction(C.select(1, 3))

        state = SystemState().with_tags({"C1": False, "C2": False, "C3": False})
        new_state = execute(instr, state)

        assert new_state.tags["C1"] is True
        assert new_state.tags["C2"] is True
        assert new_state.tags["C3"] is True


class TestLatchInstruction:
    """Test LATCH/SET instruction."""

    def test_latch_sets_bit_true(self):
        """LATCH sets the target bit to True."""
        from pyrung.core.instruction import LatchInstruction

        Motor = Bool("Motor")
        instr = LatchInstruction(Motor)

        state = SystemState().with_tags({"Motor": False})
        new_state = execute(instr, state)

        assert new_state.tags["Motor"] is True

    def test_latch_sets_block_range_true(self):
        """LATCH sets all bits in a selected range to True."""
        from pyrung.core.instruction import LatchInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        instr = LatchInstruction(C.select(10, 12))

        state = SystemState().with_tags({"C10": False, "C11": False, "C12": False})
        new_state = execute(instr, state)

        assert new_state.tags["C10"] is True
        assert new_state.tags["C11"] is True
        assert new_state.tags["C12"] is True


class TestResetInstruction:
    """Test RESET/UNLATCH instruction."""

    def test_reset_sets_bit_false(self):
        """RESET sets the target bit to False."""
        from pyrung.core.instruction import ResetInstruction

        Motor = Bool("Motor")
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

    def test_reset_sets_block_range_defaults(self):
        """RESET applies default values across all selected tags."""
        from pyrung.core.instruction import ResetInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        instr = ResetInstruction(DS.select(1, 2))

        state = SystemState().with_tags({"DS1": 123, "DS2": -9})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 0
        assert new_state.tags["DS2"] == 0


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

    def test_copy_clamps_to_int16_max(self):
        """COPY clamps to INT max when source is too large."""
        from pyrung.core.instruction import CopyInstruction

        Target = Int("Target")
        # 70000 exceeds INT max (32767)
        instr = CopyInstruction(source=70000, target=Target)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == 32767

    def test_copy_clamps_to_int16_min(self):
        """COPY clamps to INT min when source is too small."""
        from pyrung.core.instruction import CopyInstruction

        Target = Int("Target")
        # -70000 is below INT min (-32768)
        instr = CopyInstruction(source=-70000, target=Target)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == -32768

    def test_copy_clamps_to_dint32_max(self):
        """COPY clamps to DINT max when source is too large."""
        from pyrung.core.instruction import CopyInstruction
        from pyrung.core.tag import Dint

        Target = Dint("Target")
        # 2^31 exceeds DINT max (2147483647)
        instr = CopyInstruction(source=2_147_483_648, target=Target)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == 2_147_483_647

    def test_copy_clamps_to_dint32_min(self):
        """COPY clamps to DINT min when source is too small."""
        from pyrung.core.instruction import CopyInstruction
        from pyrung.core.tag import Dint

        Target = Dint("Target")
        # One below DINT min
        instr = CopyInstruction(source=-2_147_483_649, target=Target)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == -2_147_483_648

    def test_copy_word_preserves_bit_pattern_behavior(self):
        """COPY to WORD preserves low 16-bit pattern (non-clamping behavior)."""
        from pyrung.core.instruction import CopyInstruction
        from pyrung.core.tag import Word

        Target = Word("Target")

        state = SystemState()
        neg_state = execute(CopyInstruction(source=-1, target=Target), state)
        assert neg_state.tags["Target"] == 65535

        large_state = execute(CopyInstruction(source=70000, target=Target), state)
        assert large_state.tags["Target"] == 4464


class TestOneShotBehavior:
    """Test one-shot instruction behavior."""

    def test_out_oneshot_executes_once(self):
        """One-shot OUT only executes on first call per rung activation."""
        from pyrung.core.instruction import OutInstruction

        Light = Bool("Light")
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

        Light = Bool("Light")
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

        Light = Bool("Light")
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

    def test_blockcopy_clamps_to_int_dest_type(self):
        """BLOCKCOPY clamps signed overflow when destination is INT."""
        from pyrung.core.instruction import BlockCopyInstruction

        DD = Block("DD", TagType.DINT, 1, 100)
        DS = Block("DS", TagType.INT, 1, 100)

        instr = BlockCopyInstruction(DD.select(1, 2), DS.select(1, 2))

        state = SystemState().with_tags({"DD1": 70000, "DD2": -70000})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 32767
        assert new_state.tags["DS2"] == -32768

    def test_blockcopy_clamps_to_dint_dest_type(self):
        """BLOCKCOPY clamps signed overflow when destination is DINT."""
        from pyrung.core.instruction import BlockCopyInstruction

        DD = Block("DD", TagType.DINT, 1, 100)

        instr = BlockCopyInstruction(DD.select(1, 2), DD.select(10, 11))

        state = SystemState().with_tags({"DD1": 3_000_000_000, "DD2": -3_000_000_000})
        new_state = execute(instr, state)

        assert new_state.tags["DD10"] == 2_147_483_647
        assert new_state.tags["DD11"] == -2_147_483_648


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

    def test_fill_clamps_to_int_dest_type(self):
        """FILL clamps value to destination INT range."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(70000, DS.select(1, 3))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 32767
        assert new_state.tags["DS2"] == 32767
        assert new_state.tags["DS3"] == 32767

    def test_fill_clamps_negative_to_int_dest_type(self):
        """FILL clamps negative overflow to destination INT min."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        instr = FillInstruction(-70000, DS.select(1, 3))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == -32768
        assert new_state.tags["DS2"] == -32768
        assert new_state.tags["DS3"] == -32768

    def test_fill_clamps_to_dint_dest_type(self):
        """FILL clamps value to destination DINT range."""
        from pyrung.core.instruction import FillInstruction

        DD = Block("DD", TagType.DINT, 1, 100)

        instr = FillInstruction(3_000_000_000, DD.select(1, 2))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DD1"] == 2_147_483_647
        assert new_state.tags["DD2"] == 2_147_483_647

    def test_fill_clamps_negative_to_dint_dest_type(self):
        """FILL clamps negative overflow to destination DINT min."""
        from pyrung.core.instruction import FillInstruction

        DD = Block("DD", TagType.DINT, 1, 100)

        instr = FillInstruction(-3_000_000_000, DD.select(1, 2))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DD1"] == -2_147_483_648
        assert new_state.tags["DD2"] == -2_147_483_648


class TestPackBitsInstruction:
    """Test PACK_BITS instruction."""

    def test_pack_8_bits_into_int(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")
        instr = PackBitsInstruction(C.select(1, 8), Dest)

        state = SystemState().with_tags(
            {
                "C1": True,
                "C2": False,
                "C3": True,
                "C4": True,
                "C5": False,
                "C6": False,
                "C7": True,
                "C8": False,
            }
        )
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == 77

    def test_pack_16_bits_into_word(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Word("Dest")
        instr = PackBitsInstruction(C.select(1, 16), Dest)

        state = SystemState().with_tags({"C1": True, "C16": True})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == 0x8001

    def test_pack_16_bits_into_int_uses_signed_wrap(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")
        instr = PackBitsInstruction(C.select(1, 16), Dest)

        state = SystemState().with_tags({"C16": True})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == -32768

    def test_pack_32_bits_into_dint(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Dint("Dest")
        instr = PackBitsInstruction(C.select(1, 32), Dest)

        state = SystemState().with_tags({"C1": True, "C32": True})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == -2_147_483_647

    def test_pack_32_bits_into_real_reinterprets_ieee754(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Real("Dest")
        instr = PackBitsInstruction(C.select(1, 32), Dest)

        pattern = 0x3F800000  # 1.0f
        tags = {f"C{i + 1}": bool((pattern >> i) & 1) for i in range(32)}
        state = SystemState().with_tags(tags)
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == pytest.approx(1.0)

    def test_pack_uses_defaults_for_missing_bits(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")
        instr = PackBitsInstruction(C.select(1, 8), Dest)

        new_state = execute(instr, SystemState())
        assert new_state.tags["Dest"] == 0

    def test_pack_bits_17_into_16_bit_dest_raises(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")
        instr = PackBitsInstruction(C.select(1, 17), Dest)

        with pytest.raises(ValueError, match="width is 16"):
            execute(instr, SystemState())

    def test_pack_bits_33_into_32_bit_dest_raises(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Dint("Dest")
        instr = PackBitsInstruction(C.select(1, 33), Dest)

        with pytest.raises(ValueError, match="width is 32"):
            execute(instr, SystemState())

    def test_pack_bits_invalid_dest_type_raises(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Bool("Dest")
        instr = PackBitsInstruction(C.select(1, 8), Dest)

        with pytest.raises(TypeError, match="destination"):
            execute(instr, SystemState())

    def test_pack_bits_oneshot(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")
        instr = PackBitsInstruction(C.select(1, 4), Dest, oneshot=True)

        state = SystemState().with_tags({"C1": True})
        new_state = execute(instr, state)
        assert new_state.tags["Dest"] == 1

        state2 = new_state.with_tags({"C2": True, "Dest": 0})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["Dest"] == 0

    def test_pack_bits_does_not_mutate_input(self):
        from pyrung.core.instruction import PackBitsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Int("Dest")
        instr = PackBitsInstruction(C.select(1, 4), Dest)

        original = SystemState().with_tags({"C1": True, "Dest": 0})
        new_state = execute(instr, original)

        assert original.tags["Dest"] == 0
        assert new_state.tags["Dest"] == 1


class TestPackWordsInstruction:
    """Test PACK_WORDS instruction."""

    def test_pack_two_ints_into_dint_low_word_first(self):
        from pyrung.core.instruction import PackWordsInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        Dest = Dint("Dest")
        instr = PackWordsInstruction(DS.select(1, 2), Dest)

        state = SystemState().with_tags({"DS1": 0x1234, "DS2": 0x5678})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == 0x56781234

    def test_pack_two_words_into_dint(self):
        from pyrung.core.instruction import PackWordsInstruction

        DH = Block("DH", TagType.WORD, 1, 100)
        Dest = Dint("Dest")
        instr = PackWordsInstruction(DH.select(1, 2), Dest)

        state = SystemState().with_tags({"DH1": 0xFFFF, "DH2": 0x0001})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == 0x0001FFFF

    def test_pack_words_into_real_reinterprets_ieee754(self):
        from pyrung.core.instruction import PackWordsInstruction

        DH = Block("DH", TagType.WORD, 1, 100)
        Dest = Real("Dest")
        instr = PackWordsInstruction(DH.select(1, 2), Dest)

        state = SystemState().with_tags({"DH1": 0x0000, "DH2": 0x3F80})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == pytest.approx(1.0)

    def test_pack_words_negative_high_word_preserves_bit_pattern(self):
        from pyrung.core.instruction import PackWordsInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        Dest = Dint("Dest")
        instr = PackWordsInstruction(DS.select(1, 2), Dest)

        state = SystemState().with_tags({"DS1": 0, "DS2": -1})
        new_state = execute(instr, state)

        assert new_state.tags["Dest"] == -65536

    def test_pack_words_length_not_two_raises(self):
        from pyrung.core.instruction import PackWordsInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        Dest = Dint("Dest")
        instr = PackWordsInstruction(DS.select(1, 3), Dest)

        with pytest.raises(ValueError, match="exactly 2"):
            execute(instr, SystemState())

    def test_pack_words_invalid_dest_type_raises(self):
        from pyrung.core.instruction import PackWordsInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        Dest = Int("Dest")
        instr = PackWordsInstruction(DS.select(1, 2), Dest)

        with pytest.raises(TypeError, match="destination"):
            execute(instr, SystemState())

    def test_pack_words_invalid_source_type_raises(self):
        from pyrung.core.instruction import PackWordsInstruction

        C = Block("C", TagType.BOOL, 1, 100)
        Dest = Dint("Dest")
        instr = PackWordsInstruction(C.select(1, 2), Dest)

        with pytest.raises(TypeError, match="source tags"):
            execute(instr, SystemState())

    def test_pack_words_oneshot(self):
        from pyrung.core.instruction import PackWordsInstruction

        DS = Block("DS", TagType.INT, 1, 100)
        Dest = Dint("Dest")
        instr = PackWordsInstruction(DS.select(1, 2), Dest, oneshot=True)

        state = SystemState().with_tags({"DS1": 1, "DS2": 0})
        new_state = execute(instr, state)
        assert new_state.tags["Dest"] == 1

        state2 = new_state.with_tags({"DS1": 2, "Dest": 0})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["Dest"] == 0


class TestUnpackToBitsInstruction:
    """Test UNPACK_TO_BITS instruction."""

    def test_unpack_int_to_16_bits(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Int("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 16))

        state = SystemState().with_tags({"Source": 0x00A5})
        new_state = execute(instr, state)

        assert new_state.tags["C1"] is True
        assert new_state.tags["C2"] is False
        assert new_state.tags["C3"] is True
        assert new_state.tags["C4"] is False
        assert new_state.tags["C5"] is False
        assert new_state.tags["C6"] is True
        assert new_state.tags["C7"] is False
        assert new_state.tags["C8"] is True

    def test_unpack_negative_int_sets_bit_15(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Int("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 16))

        state = SystemState().with_tags({"Source": -32768})
        new_state = execute(instr, state)

        assert new_state.tags["C16"] is True
        assert new_state.tags["C1"] is False

    def test_unpack_dint_to_32_bits(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Dint("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 32))

        state = SystemState().with_tags({"Source": -2_147_483_647})  # 0x80000001
        new_state = execute(instr, state)

        assert new_state.tags["C1"] is True
        assert new_state.tags["C32"] is True
        assert new_state.tags["C2"] is False

    def test_unpack_real_to_32_bits_uses_ieee754_pattern(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Real("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 32))

        state = SystemState().with_tags({"Source": 1.0})
        new_state = execute(instr, state)

        bits = 0
        for i in range(32):
            if new_state.tags[f"C{i + 1}"]:
                bits |= 1 << i
        assert bits == 0x3F800000

    def test_unpack_zero_sets_all_false(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Dint("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 8))

        state = SystemState().with_tags({"Source": 0})
        new_state = execute(instr, state)

        for i in range(1, 9):
            assert new_state.tags[f"C{i}"] is False

    def test_unpack_all_ones_sets_all_true(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Dint("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 32))

        state = SystemState().with_tags({"Source": -1})
        new_state = execute(instr, state)

        for i in range(1, 33):
            assert new_state.tags[f"C{i}"] is True

    def test_unpack_int_to_17_bits_raises(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Int("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 17))

        with pytest.raises(ValueError, match="width is 16"):
            execute(instr, SystemState().with_tags({"Source": 0}))

    def test_unpack_dint_to_33_bits_raises(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Dint("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 33))

        with pytest.raises(ValueError, match="width is 32"):
            execute(instr, SystemState().with_tags({"Source": 0}))

    def test_unpack_to_bits_invalid_source_type_raises(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Bool("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 8))

        with pytest.raises(TypeError, match="source"):
            execute(instr, SystemState())

    def test_unpack_to_bits_oneshot(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Int("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 4), oneshot=True)

        state = SystemState().with_tags({"Source": 1, "C1": False, "C2": False})
        new_state = execute(instr, state)
        assert new_state.tags["C1"] is True

        state2 = new_state.with_tags({"Source": 2, "C1": False, "C2": False})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["C1"] is False
        assert new_state2.tags["C2"] is False

    def test_unpack_to_bits_does_not_mutate_input(self):
        from pyrung.core.instruction import UnpackToBitsInstruction

        Source = Int("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToBitsInstruction(Source, C.select(1, 4))

        original = SystemState().with_tags({"Source": 1, "C1": False})
        new_state = execute(instr, original)

        assert original.tags["C1"] is False
        assert new_state.tags["C1"] is True


class TestUnpackToWordsInstruction:
    """Test UNPACK_TO_WORDS instruction."""

    def test_unpack_dint_to_two_words_low_word_first(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Dint("Source")
        DS = Block("DS", TagType.INT, 1, 100)
        instr = UnpackToWordsInstruction(Source, DS.select(1, 2))

        state = SystemState().with_tags({"Source": 0x56781234})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 0x1234
        assert new_state.tags["DS2"] == 0x5678

    def test_unpack_real_to_two_words(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Real("Source")
        DH = Block("DH", TagType.WORD, 1, 100)
        instr = UnpackToWordsInstruction(Source, DH.select(1, 2))

        state = SystemState().with_tags({"Source": 1.0})
        new_state = execute(instr, state)

        assert new_state.tags["DH1"] == 0x0000
        assert new_state.tags["DH2"] == 0x3F80

    def test_unpack_negative_dint_source(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Dint("Source")
        DH = Block("DH", TagType.WORD, 1, 100)
        instr = UnpackToWordsInstruction(Source, DH.select(1, 2))

        state = SystemState().with_tags({"Source": -1})
        new_state = execute(instr, state)

        assert new_state.tags["DH1"] == 0xFFFF
        assert new_state.tags["DH2"] == 0xFFFF

    def test_unpack_to_words_wraps_for_int_dest(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Dint("Source")
        DS = Block("DS", TagType.INT, 1, 100)
        instr = UnpackToWordsInstruction(Source, DS.select(1, 2))

        state = SystemState().with_tags({"Source": -1})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == -1
        assert new_state.tags["DS2"] == -1

    def test_unpack_to_words_length_not_two_raises(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Dint("Source")
        DS = Block("DS", TagType.INT, 1, 100)
        instr = UnpackToWordsInstruction(Source, DS.select(1, 3))

        with pytest.raises(ValueError, match="exactly 2"):
            execute(instr, SystemState().with_tags({"Source": 0}))

    def test_unpack_to_words_invalid_source_type_raises(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Int("Source")
        DS = Block("DS", TagType.INT, 1, 100)
        instr = UnpackToWordsInstruction(Source, DS.select(1, 2))

        with pytest.raises(TypeError, match="source"):
            execute(instr, SystemState())

    def test_unpack_to_words_invalid_dest_type_raises(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Dint("Source")
        C = Block("C", TagType.BOOL, 1, 100)
        instr = UnpackToWordsInstruction(Source, C.select(1, 2))

        with pytest.raises(TypeError, match="destination tags"):
            execute(instr, SystemState().with_tags({"Source": 0}))

    def test_unpack_to_words_oneshot(self):
        from pyrung.core.instruction import UnpackToWordsInstruction

        Source = Dint("Source")
        DS = Block("DS", TagType.INT, 1, 100)
        instr = UnpackToWordsInstruction(Source, DS.select(1, 2), oneshot=True)

        state = SystemState().with_tags({"Source": 1, "DS1": 0, "DS2": 0})
        new_state = execute(instr, state)
        assert new_state.tags["DS1"] == 1
        assert new_state.tags["DS2"] == 0

        state2 = new_state.with_tags({"Source": 2, "DS1": 0, "DS2": 0})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["DS1"] == 0
        assert new_state2.tags["DS2"] == 0


class TestPackUnpackRoundTrip:
    """Round-trip validation for pack/unpack instruction pairs."""

    def test_pack_bits_then_unpack_to_bits_recovers_pattern(self):
        from pyrung.core.instruction import PackBitsInstruction, UnpackToBitsInstruction

        CIn = Block("CIn", TagType.BOOL, 1, 100)
        COut = Block("COut", TagType.BOOL, 1, 100)
        Temp = Dint("Temp")

        pattern = 0xA5A5A5A5
        source_tags = {f"CIn{i + 1}": bool((pattern >> i) & 1) for i in range(32)}
        initial_state = SystemState().with_tags(source_tags)

        packed_state = execute(PackBitsInstruction(CIn.select(1, 32), Temp), initial_state)
        roundtrip_state = execute(
            UnpackToBitsInstruction(Temp, COut.select(1, 32)),
            packed_state,
        )

        for i in range(32):
            assert roundtrip_state.tags[f"COut{i + 1}"] == source_tags[f"CIn{i + 1}"]

    def test_pack_words_then_unpack_to_words_through_real_recovers_values(self):
        from pyrung.core.instruction import PackWordsInstruction, UnpackToWordsInstruction

        DSIn = Block("DSIn", TagType.INT, 1, 100)
        DSOut = Block("DSOut", TagType.INT, 1, 100)
        Temp = Real("Temp")

        initial_state = SystemState().with_tags({"DSIn1": -12345, "DSIn2": 23456})
        packed_state = execute(PackWordsInstruction(DSIn.select(1, 2), Temp), initial_state)
        roundtrip_state = execute(
            UnpackToWordsInstruction(Temp, DSOut.select(1, 2)),
            packed_state,
        )

        assert roundtrip_state.tags["DSOut1"] == -12345
        assert roundtrip_state.tags["DSOut2"] == 23456

        bits = struct.unpack("<I", struct.pack("<f", packed_state.tags["Temp"]))[0]
        assert bits == ((23456 << 16) | ((-12345) & 0xFFFF))


class TestMathInstruction:
    """Test MATH instruction — hardware-verified overflow and truncation behavior."""

    def test_math_simple_addition(self):
        """MATH evaluates expression and stores result."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Int("Result")
        expr = TagExpr(DS1) + TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        state = SystemState().with_tags({"DS1": 100, "DS2": 200})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == 300

    def test_math_truncation_int16(self):
        """INT dest truncates to 16-bit signed (modular wrapping)."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Int("Result")
        expr = TagExpr(DS1) * TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        # 1000 * 1000 = 1,000,000 → mod 65536 = 16960
        # As signed 16-bit: 16960 (fits in positive range)
        state = SystemState().with_tags({"DS1": 1000, "DS2": 1000})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 16960

    def test_math_truncation_int16_signed_wrap(self):
        """INT dest wraps to negative for values above 32767."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Int("Result")
        expr = TagExpr(DS1) + TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        # 30000 + 30000 = 60000 → signed 16-bit: -5536
        state = SystemState().with_tags({"DS1": 30000, "DS2": 30000})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == -5536

    def test_math_truncation_dint32(self):
        """DINT dest truncates to 32-bit signed."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction
        from pyrung.core.tag import Dint

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Dint("Result")
        expr = TagExpr(DS1) * TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        # 1000 * 1000 = 1,000,000 — fits in 32-bit signed
        state = SystemState().with_tags({"DS1": 1000, "DS2": 1000})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 1_000_000

    def test_math_dint32_overflow_wrap(self):
        """DINT wraps at 32-bit signed boundary (hardware-verified)."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction
        from pyrung.core.tag import Dint

        DD1 = Dint("DD1")
        Result = Dint("Result")
        # 2,147,483,647 + 1 = -2,147,483,648
        expr = TagExpr(DD1) + 1
        instr = MathInstruction(expr, Result)

        state = SystemState().with_tags({"DD1": 2_147_483_647})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == -2_147_483_648

    def test_math_dint32_multiply_wrap(self):
        """50000 * 50000 wraps in 32-bit signed (hardware-verified)."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction
        from pyrung.core.tag import Dint

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Dint("Result")
        expr = TagExpr(DS1) * TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        # 50000 * 50000 = 2,500,000,000 → wraps to -1,794,967,296
        state = SystemState().with_tags({"DS1": 50000, "DS2": 50000})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == -1_794_967_296

    def test_math_division_by_zero(self):
        """Division by zero produces 0."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Int("Result")
        expr = TagExpr(DS1) / TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        state = SystemState().with_tags({"DS1": 100, "DS2": 0, "Result": 999})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 0

    def test_math_integer_division_truncates_toward_zero(self):
        """Division truncates toward zero: -7 / 2 = -3 (not -4)."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Int("Result")
        # True division gives -3.5, int() gives -3
        expr = TagExpr(DS1) / TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        state = SystemState().with_tags({"DS1": -7, "DS2": 2})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == -3

    def test_math_real_dest_no_truncation(self):
        """REAL destination stores float without integer truncation."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction
        from pyrung.core.tag import Real

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        Result = Real("Result")
        expr = TagExpr(DS1) / TagExpr(DS2)
        instr = MathInstruction(expr, Result)

        state = SystemState().with_tags({"DS1": 7, "DS2": 2})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 3.5

    def test_math_hex_mode_unsigned_wrap(self):
        """Hex mode wraps at unsigned 16-bit boundary (0-65535)."""
        from pyrung.core.instruction import MathInstruction
        from pyrung.core.tag import Word

        MaskA = Word("MaskA")
        Result = Word("Result")
        # 0xFFFF + 1 = 0x10000 → wraps to 0
        instr = MathInstruction(MaskA + 1, Result, mode="hex")

        state = SystemState().with_tags({"MaskA": 0xFFFF})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 0

    def test_math_hex_mode_no_sign_extension(self):
        """Hex mode result is always 0-65535 (no sign)."""
        from pyrung.core.instruction import MathInstruction
        from pyrung.core.tag import Word

        MaskA = Word("MaskA")
        Result = Word("Result")
        # -1 in hex mode → 0xFFFF = 65535
        instr = MathInstruction(MaskA - 2, Result, mode="hex")

        state = SystemState().with_tags({"MaskA": 1})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 65535

    def test_math_literal_expression(self):
        """MATH works with literal values."""
        from pyrung.core.instruction import MathInstruction

        Result = Int("Result")
        instr = MathInstruction(42, Result)

        state = SystemState()
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 42

    def test_math_oneshot(self):
        """MATH with oneshot only executes once."""
        from pyrung.core.instruction import MathInstruction

        Result = Int("Result")
        instr = MathInstruction(42, Result, oneshot=True)

        state = SystemState()
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 42

        # Second execution — oneshot blocks it
        state2 = new_state.with_tags({"Result": 0})
        new_state2 = execute(instr, state2)
        assert new_state2.tags["Result"] == 0  # Unchanged

    def test_math_does_not_mutate_input(self):
        """MATH returns new state, input unchanged."""
        from pyrung.core.instruction import MathInstruction

        Result = Int("Result")
        instr = MathInstruction(42, Result)

        original = SystemState().with_tags({"Result": 0})
        new_state = execute(instr, original)

        assert original.tags["Result"] == 0
        assert new_state.tags["Result"] == 42

    def test_math_spec_example_ds_overflow(self):
        """Spec example: 200*200+30000=70000 → DS stores 4464."""
        from pyrung.core.expression import TagExpr
        from pyrung.core.instruction import MathInstruction

        DS1 = Int("DS1")
        DS2 = Int("DS2")
        DS3 = Int("DS3")
        Result = Int("Result")
        expr = TagExpr(DS1) * TagExpr(DS2) + TagExpr(DS3)
        instr = MathInstruction(expr, Result)

        state = SystemState().with_tags({"DS1": 200, "DS2": 200, "DS3": 30000})
        new_state = execute(instr, state)
        assert new_state.tags["Result"] == 4464


class TestSearchInstruction:
    """Test SEARCH instruction."""

    def test_search_found_must_be_bool(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Result = Int("Result")

        with pytest.raises(TypeError, match="found tag must be BOOL"):
            SearchInstruction(
                condition="==",
                value=1,
                search_range=DS.select(1, 3),
                result=Result,
                found=Int("Found"),
            )

    def test_search_result_must_be_int_or_dint(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")

        with pytest.raises(TypeError, match="result tag must be INT or DINT"):
            SearchInstruction(
                condition="==",
                value=1,
                search_range=DS.select(1, 3),
                result=Real("Result"),
                found=Found,
            )

    def test_search_invalid_condition_rejected(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        with pytest.raises(ValueError, match="Invalid search condition"):
            SearchInstruction(
                condition="=",
                value=1,
                search_range=DS.select(1, 3),
                result=Result,
                found=Found,
            )

    def test_search_range_type_enforced(self):
        from pyrung.core.instruction import SearchInstruction

        Found = Bool("Found")
        Result = Int("Result")

        with pytest.raises(
            TypeError, match="search_range must be BlockRange or IndirectBlockRange"
        ):
            SearchInstruction(
                condition="==",
                value=1,
                search_range=Int("NotARange"),  # type: ignore[arg-type]
                result=Result,
                found=Found,
            )

    @pytest.mark.parametrize(
        ("condition", "value", "expected"),
        [
            ("==", 10, 1),
            ("!=", 10, 2),
            (">", 10, 2),
            (">=", 20, 2),
            ("<", 10, 3),
            ("<=", 10, 1),
        ],
    )
    def test_search_numeric_success(self, condition, value, expected):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction(condition, value, DS.select(1, 3), Result, Found)
        state = SystemState().with_tags({"DS1": 10, "DS2": 20, "DS3": 5})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == expected
        assert new_state.tags["Found"] is True

    def test_search_numeric_miss_sets_minus_one_and_false(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction(">", 100, DS.select(1, 3), Result, Found)
        state = SystemState().with_tags({"DS1": 1, "DS2": 2, "DS3": 3, "Result": 99, "Found": True})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == -1
        assert new_state.tags["Found"] is False

    def test_search_continuous_progression(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", 2, DS.select(1, 4), Result, Found, continuous=True)
        state = SystemState().with_tags(
            {"DS1": 1, "DS2": 2, "DS3": 3, "DS4": 2, "Result": 0, "Found": False}
        )

        step1 = execute(instr, state)
        assert step1.tags["Result"] == 2
        assert step1.tags["Found"] is True

        step2 = execute(instr, step1)
        assert step2.tags["Result"] == 4
        assert step2.tags["Found"] is True

        step3 = execute(instr, step2)
        assert step3.tags["Result"] == -1
        assert step3.tags["Found"] is False

    def test_search_continuous_exhausted_no_rescan(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", 1, DS.select(1, 3), Result, Found, continuous=True)
        state = SystemState().with_tags({"DS1": 1, "DS2": 1, "DS3": 1, "Result": -1, "Found": True})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == -1
        assert new_state.tags["Found"] is False

    def test_search_continuous_restart_when_result_zero(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", 7, DS.select(1, 3), Result, Found, continuous=True)
        state = SystemState().with_tags({"DS1": 1, "DS2": 7, "DS3": 7, "Result": 0, "Found": False})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == 2
        assert new_state.tags["Found"] is True

    def test_search_continuous_reverse_resume(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction(
            "==", 5, DS.select(1, 5).reverse(), Result, Found, continuous=True
        )
        state = SystemState().with_tags(
            {"DS1": 0, "DS2": 5, "DS3": 0, "DS4": 5, "DS5": 0, "Result": 0, "Found": False}
        )

        step1 = execute(instr, state)
        assert step1.tags["Result"] == 4
        assert step1.tags["Found"] is True

        step2 = execute(instr, step1)
        assert step2.tags["Result"] == 2
        assert step2.tags["Found"] is True

        step3 = execute(instr, step2)
        assert step3.tags["Result"] == -1
        assert step3.tags["Found"] is False

    def test_search_oneshot_behavior(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", 2, DS.select(1, 2), Result, Found, oneshot=True)
        state = SystemState().with_tags({"DS1": 0, "DS2": 2, "Result": 0, "Found": False})

        step1 = execute(instr, state)
        assert step1.tags["Result"] == 2
        assert step1.tags["Found"] is True

        step2 = execute(instr, step1.with_tags({"Result": 77, "Found": False, "DS2": 0}))
        assert step2.tags["Result"] == 77
        assert step2.tags["Found"] is False

    def test_search_rung_false_preserves_result_and_found(self):
        from pyrung.core.instruction import SearchInstruction
        from pyrung.core.rung import Rung as RungLogic

        Enable = Bool("Enable")
        DS = Block("DS", TagType.INT, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        rung = RungLogic(Enable)
        rung.add_instruction(SearchInstruction("==", 1, DS.select(1, 3), Result, Found))

        state = SystemState().with_tags(
            {"Enable": False, "DS1": 1, "DS2": 1, "DS3": 1, "Result": 55, "Found": True}
        )
        new_state = evaluate_rung(rung, state)

        assert new_state.tags["Result"] == 55
        assert new_state.tags["Found"] is True

    def test_search_text_equality(self):
        from pyrung.core.instruction import SearchInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", "ADC", CH.select(1, 6), Result, Found)
        state = SystemState().with_tags({"CH1": "A", "CH2": "D", "CH3": "C", "CH4": "X"})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == 1
        assert new_state.tags["Found"] is True

    def test_search_text_inequality(self):
        from pyrung.core.instruction import SearchInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("!=", "ADC", CH.select(1, 4), Result, Found)
        state = SystemState().with_tags({"CH1": "A", "CH2": "D", "CH3": "C", "CH4": "X"})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == 2
        assert new_state.tags["Found"] is True

    def test_search_text_reverse_range(self):
        from pyrung.core.instruction import SearchInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", "ADC", CH.select(1, 4).reverse(), Result, Found)
        state = SystemState().with_tags({"CH1": "Z", "CH2": "C", "CH3": "D", "CH4": "A"})
        new_state = execute(instr, state)

        assert new_state.tags["Result"] == 4
        assert new_state.tags["Found"] is True

    def test_search_text_invalid_operator(self):
        from pyrung.core.instruction import SearchInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")
        instr = SearchInstruction(">", "ADC", CH.select(1, 4), Result, Found)

        with pytest.raises(ValueError, match="Text search only supports"):
            execute(instr, SystemState().with_tags({"CH1": "A", "CH2": "B", "CH3": "C"}))

    def test_search_text_empty_value_rejected(self):
        from pyrung.core.instruction import SearchInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Found = Bool("Found")
        Result = Int("Result")
        instr = SearchInstruction("==", "", CH.select(1, 4), Result, Found)

        with pytest.raises(ValueError, match="cannot be empty"):
            execute(instr, SystemState().with_tags({"CH1": "A", "CH2": "B"}))

    def test_search_empty_resolved_range_is_miss(self):
        from pyrung.core.instruction import SearchInstruction

        DS = Block("DS", TagType.INT, 1, 10, valid_ranges=((1, 2), (5, 6)))
        Found = Bool("Found")
        Result = Int("Result")

        instr = SearchInstruction("==", 0, DS.select(3, 4), Result, Found)
        new_state = execute(instr, SystemState().with_tags({"Result": 123, "Found": True}))

        assert new_state.tags["Result"] == -1
        assert new_state.tags["Found"] is False


class TestCopyTextModifiers:
    def test_copy_as_value_expands_sequential_destinations(self):
        from pyrung.core.copy_modifiers import as_value
        from pyrung.core.instruction import CopyInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        instr = CopyInstruction(as_value("123"), DS[1])
        new_state = execute(instr, SystemState())

        assert new_state.tags["DS1"] == 1
        assert new_state.tags["DS2"] == 2
        assert new_state.tags["DS3"] == 3

    def test_copy_as_value_non_digit_sets_out_of_range_and_skips_write(self):
        from pyrung.core.copy_modifiers import as_value
        from pyrung.core.instruction import CopyInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        instr = CopyInstruction(as_value("1A3"), DS[1])
        new_state = execute(instr, SystemState().with_tags({"DS1": 9, "DS2": 9, "DS3": 9}))

        assert new_state.tags["fault.out_of_range"] is True
        assert new_state.tags["DS1"] == 9
        assert new_state.tags["DS2"] == 9
        assert new_state.tags["DS3"] == 9

    def test_copy_as_ascii_converts_char_codes(self):
        from pyrung.core.copy_modifiers import as_ascii
        from pyrung.core.instruction import CopyInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        instr = CopyInstruction(as_ascii("AZ"), DS[1])
        new_state = execute(instr, SystemState())

        assert new_state.tags["DS1"] == 65
        assert new_state.tags["DS2"] == 90

    def test_copy_as_text_do_not_suppress_zero(self):
        from pyrung.core.copy_modifiers import as_text
        from pyrung.core.instruction import CopyInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Source = Int("Source")
        instr = CopyInstruction(as_text(Source, suppress_zero=False), CH[1])
        new_state = execute(instr, SystemState().with_tags({"Source": 123}))

        assert new_state.tags["CH1"] == "0"
        assert new_state.tags["CH2"] == "0"
        assert new_state.tags["CH3"] == "1"
        assert new_state.tags["CH4"] == "2"
        assert new_state.tags["CH5"] == "3"

    def test_copy_as_text_with_termination_code(self):
        from pyrung.core.copy_modifiers import as_text
        from pyrung.core.instruction import CopyInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Source = Int("Source")
        instr = CopyInstruction(as_text(Source, termination_code=13), CH[1])
        new_state = execute(instr, SystemState().with_tags({"Source": 5}))

        assert new_state.tags["CH1"] == "5"
        assert ord(new_state.tags["CH2"]) == 13

    def test_copy_as_binary_low_byte_ascii(self):
        from pyrung.core.copy_modifiers import as_binary
        from pyrung.core.instruction import CopyInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Source = Int("Source")
        instr = CopyInstruction(as_binary(Source), CH[1])
        new_state = execute(instr, SystemState().with_tags({"Source": 123}))

        assert new_state.tags["CH1"] == "{"

    def test_copy_pointer_resolution_error_sets_address_error_only(self):
        from pyrung.core.copy_modifiers import as_binary
        from pyrung.core.instruction import CopyInstruction

        DS = Block("DS", TagType.INT, 1, 10)
        Pointer = Int("Pointer")
        CH = Block("CH", TagType.CHAR, 1, 10)
        instr = CopyInstruction(as_binary(DS[Pointer]), CH[1])
        new_state = execute(instr, SystemState().with_tags({"Pointer": 999}))

        assert new_state.tags["fault.address_error"] is True
        assert new_state.tags.get("fault.out_of_range", False) is False
        assert "CH1" not in new_state.tags


class TestBlockCopyTextModes:
    def test_blockcopy_as_value_text_to_numeric(self):
        from pyrung.core.copy_modifiers import as_value
        from pyrung.core.instruction import BlockCopyInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        DS = Block("DS", TagType.INT, 1, 10)
        instr = BlockCopyInstruction(as_value(CH.select(1, 3)), DS.select(1, 3))
        state = SystemState().with_tags({"CH1": "1", "CH2": "2", "CH3": "3"})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 1
        assert new_state.tags["DS2"] == 2
        assert new_state.tags["DS3"] == 3

    def test_blockcopy_as_value_failure_sets_out_of_range_no_partial_write(self):
        from pyrung.core.copy_modifiers import as_value
        from pyrung.core.instruction import BlockCopyInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        DS = Block("DS", TagType.INT, 1, 10)
        instr = BlockCopyInstruction(as_value(CH.select(1, 3)), DS.select(1, 3))
        state = SystemState().with_tags(
            {"CH1": "1", "CH2": "A", "CH3": "3", "DS1": 9, "DS2": 9, "DS3": 9}
        )
        new_state = execute(instr, state)

        assert new_state.tags["fault.out_of_range"] is True
        assert new_state.tags["DS1"] == 9
        assert new_state.tags["DS2"] == 9
        assert new_state.tags["DS3"] == 9
        assert new_state.tags.get("fault.address_error", False) is False


class TestPackTextInstruction:
    def test_pack_text_parses_int(self):
        from pyrung.core.instruction import PackTextInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Dest = Int("Dest")
        instr = PackTextInstruction(CH.select(1, 3), Dest)
        state = SystemState().with_tags({"CH1": "1", "CH2": "2", "CH3": "3"})
        new_state = execute(instr, state)
        assert new_state.tags["Dest"] == 123

    def test_pack_text_real_exponential(self):
        from pyrung.core.instruction import PackTextInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Dest = Real("Dest")
        instr = PackTextInstruction(CH.select(1, 6), Dest)
        state = SystemState().with_tags(
            {"CH1": "1", "CH2": "e", "CH3": "-", "CH4": "2", "CH5": "", "CH6": ""}
        )
        new_state = execute(instr, state)
        assert new_state.tags["Dest"] == pytest.approx(0.01)

    def test_pack_text_word_hex(self):
        from pyrung.core.instruction import PackTextInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Dest = Word("Dest")
        instr = PackTextInstruction(CH.select(1, 4), Dest)
        state = SystemState().with_tags({"CH1": "A", "CH2": "B", "CH3": "C", "CH4": "D"})
        new_state = execute(instr, state)
        assert new_state.tags["Dest"] == 0xABCD

    def test_pack_text_whitespace_rejected_without_option(self):
        from pyrung.core.instruction import PackTextInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Dest = Int("Dest")
        instr = PackTextInstruction(CH.select(1, 3), Dest, allow_whitespace=False)
        state = SystemState().with_tags({"CH1": " ", "CH2": "1", "CH3": "2", "Dest": 77})
        new_state = execute(instr, state)
        assert new_state.tags["fault.out_of_range"] is True
        assert new_state.tags["Dest"] == 77

    def test_pack_text_allow_whitespace_trims_without_fault(self):
        from pyrung.core.instruction import PackTextInstruction

        CH = Block("CH", TagType.CHAR, 1, 10)
        Dest = Int("Dest")
        instr = PackTextInstruction(CH.select(1, 3), Dest, allow_whitespace=True)
        state = SystemState().with_tags({"CH1": " ", "CH2": "1", "CH3": "2"})
        new_state = execute(instr, state)
        assert new_state.tags["Dest"] == 12
        assert new_state.tags.get("fault.out_of_range", False) is False
