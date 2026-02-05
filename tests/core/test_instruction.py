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

    def test_copy_truncates_to_int16(self):
        """COPY truncates value to destination INT (16-bit signed)."""
        from pyrung.core.instruction import CopyInstruction

        Target = Int("Target")
        # 70000 → signed 16-bit: 4464
        instr = CopyInstruction(source=70000, target=Target)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == 4464

    def test_copy_truncates_to_dint32(self):
        """COPY truncates value to destination DINT (32-bit signed)."""
        from pyrung.core.instruction import CopyInstruction
        from pyrung.core.tag import Dint

        Target = Dint("Target")
        # 2^31 wraps to -2147483648
        instr = CopyInstruction(source=2_147_483_648, target=Target)

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["Target"] == -2_147_483_648


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

    def test_blockcopy_truncates_to_dest_type(self):
        """BLOCKCOPY truncates values to destination type (DINT→INT)."""
        from pyrung.core.instruction import BlockCopyInstruction

        DD = Block("DD", TagType.DINT, 1, 100)
        DS = Block("DS", TagType.INT, 1, 100)

        instr = BlockCopyInstruction(DD.select(1, 2), DS.select(1, 2))

        # 70000 → INT16: 4464, 100000 → INT16: -31072
        state = SystemState().with_tags({"DD1": 70000, "DD2": 100000})
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 4464
        assert new_state.tags["DS2"] == -31072


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

    def test_fill_truncates_to_dest_type(self):
        """FILL truncates value to destination INT (16-bit signed)."""
        from pyrung.core.instruction import FillInstruction

        DS = Block("DS", TagType.INT, 1, 100)

        # 70000 → INT16: 4464
        instr = FillInstruction(70000, DS.select(1, 3))

        state = SystemState()
        new_state = execute(instr, state)

        assert new_state.tags["DS1"] == 4464
        assert new_state.tags["DS2"] == 4464
        assert new_state.tags["DS3"] == 4464


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
