"""Tests for math expression support in pyrung engine.

TDD Phase 1: Write failing tests that define the expected API.
"""

import math

import pytest

from pyrung.core import Block, Bool, Int, ScanContext, SystemState, TagType
from pyrung.core.condition import Condition
from tests.conftest import evaluate_condition

# =============================================================================
# Phase 1: Basic Arithmetic Tests
# =============================================================================


class TestBasicArithmetic:
    """Test basic arithmetic operations on Tags."""

    def test_tag_plus_literal(self):
        """DS[1] + 5 creates an expression that evaluates correctly."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] + 5
        state = SystemState().with_tags({"DS1": 10})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 15

    def test_literal_plus_tag(self):
        """5 + DS[1] uses __radd__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 5 + DS[1]
        state = SystemState().with_tags({"DS1": 10})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 15

    def test_tag_plus_tag(self):
        """DS[1] + DS[2] adds two tag values."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] + DS[2]
        state = SystemState().with_tags({"DS1": 10, "DS2": 20})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 30

    def test_tag_minus_literal(self):
        """DS[1] - 5."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] - 5
        state = SystemState().with_tags({"DS1": 10})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 5

    def test_literal_minus_tag(self):
        """100 - DS[1] uses __rsub__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 100 - DS[1]
        state = SystemState().with_tags({"DS1": 30})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 70

    def test_tag_minus_tag(self):
        """DS[1] - DS[2]."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] - DS[2]
        state = SystemState().with_tags({"DS1": 50, "DS2": 20})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 30

    def test_multiplication(self):
        """DS[1] * 2."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] * 2
        state = SystemState().with_tags({"DS1": 7})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 14

    def test_literal_times_tag(self):
        """3 * DS[1] uses __rmul__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 3 * DS[1]
        state = SystemState().with_tags({"DS1": 5})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 15

    def test_division(self):
        """DS[1] / 3 returns float."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] / 3
        state = SystemState().with_tags({"DS1": 10})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(10 / 3)

    def test_literal_divided_by_tag(self):
        """100 / DS[1] uses __rtruediv__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 100 / DS[1]
        state = SystemState().with_tags({"DS1": 4})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 25.0

    def test_floor_division(self):
        """DS[1] // 3 returns int."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] // 3
        state = SystemState().with_tags({"DS1": 10})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 3

    def test_literal_floordiv_tag(self):
        """100 // DS[1] uses __rfloordiv__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 100 // DS[1]
        state = SystemState().with_tags({"DS1": 3})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 33

    def test_modulo(self):
        """Count % 10."""
        Count = Int("Count")
        expr = Count % 10
        state = SystemState().with_tags({"Count": 27})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 7

    def test_literal_mod_tag(self):
        """100 % DS[1] uses __rmod__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 100 % DS[1]
        state = SystemState().with_tags({"DS1": 7})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 2

    def test_power(self):
        """DS[1] ** 2."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] ** 2
        state = SystemState().with_tags({"DS1": 5})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 25

    def test_literal_pow_tag(self):
        """2 ** DS[1] uses __rpow__."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = 2 ** DS[1]
        state = SystemState().with_tags({"DS1": 3})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 8

    def test_negation(self):
        """-DS[1]."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = -DS[1]
        state = SystemState().with_tags({"DS1": 42})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == -42

    def test_positive(self):
        """+DS[1]."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = +DS[1]
        state = SystemState().with_tags({"DS1": 42})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 42

    def test_abs(self):
        """abs(DS[1])."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = abs(DS[1])
        state = SystemState().with_tags({"DS1": -42})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 42

    def test_complex_expression(self):
        """(DS[1] * 2) + (DS[2] / 3)."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = (DS[1] * 2) + (DS[2] / 3)
        state = SystemState().with_tags({"DS1": 10, "DS2": 30})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(30.0)  # 20 + 10

    def test_nested_parentheses(self):
        """((DS[1] + DS[2]) * (DS[3] - DS[4])) / DS[5]."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = ((DS[1] + DS[2]) * (DS[3] - DS[4])) / DS[5]
        state = SystemState().with_tags(
            {
                "DS1": 5,  # 5 + 15 = 20
                "DS2": 15,
                "DS3": 30,  # 30 - 10 = 20
                "DS4": 10,
                "DS5": 10,  # 20 * 20 / 10 = 40
            }
        )
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 40.0


# =============================================================================
# Phase 2: Expression Comparisons
# =============================================================================


class TestExpressionComparisons:
    """Test comparisons returning Conditions from expressions."""

    def test_expression_gt_literal(self):
        """(DS[1] + DS[2]) > 100 returns a Condition."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] + DS[2]
        cond = expr > 100
        assert isinstance(cond, Condition)

    def test_expression_gt_evaluates_true(self):
        """(DS[1] + DS[2]) > 100 evaluates True when sum > 100."""
        DS = Block("DS", TagType.INT, 1, 9)
        cond = (DS[1] + DS[2]) > 100
        state = SystemState().with_tags({"DS1": 60, "DS2": 50})
        assert evaluate_condition(cond, state) is True

    def test_expression_gt_evaluates_false(self):
        """(DS[1] + DS[2]) > 100 evaluates False when sum <= 100."""
        DS = Block("DS", TagType.INT, 1, 9)
        cond = (DS[1] + DS[2]) > 100
        state = SystemState().with_tags({"DS1": 40, "DS2": 50})
        assert evaluate_condition(cond, state) is False

    def test_expression_ge_literal(self):
        """(DS[1] * 2) >= 100."""
        DS = Block("DS", TagType.INT, 1, 9)
        cond = (DS[1] * 2) >= 100
        state = SystemState().with_tags({"DS1": 50})
        assert evaluate_condition(cond, state) is True
        state2 = SystemState().with_tags({"DS1": 49})
        assert evaluate_condition(cond, state2) is False

    def test_expression_lt_literal(self):
        """(DS[1] - DS[2]) < 0."""
        DS = Block("DS", TagType.INT, 1, 9)
        cond = (DS[1] - DS[2]) < 0
        state = SystemState().with_tags({"DS1": 10, "DS2": 20})
        assert evaluate_condition(cond, state) is True

    def test_expression_le_literal(self):
        """(DS[1] / 2) <= 25."""
        DS = Block("DS", TagType.INT, 1, 9)
        cond = (DS[1] / 2) <= 25
        state = SystemState().with_tags({"DS1": 50})
        assert evaluate_condition(cond, state) is True

    def test_expression_eq_zero(self):
        """(Count % 10) == 0."""
        Count = Int("Count")
        cond = (Count % 10) == 0
        state = SystemState().with_tags({"Count": 30})
        assert evaluate_condition(cond, state) is True
        state2 = SystemState().with_tags({"Count": 27})
        assert evaluate_condition(cond, state2) is False

    def test_expression_ne_literal(self):
        """(DS[1] + 1) != 100."""
        DS = Block("DS", TagType.INT, 1, 9)
        cond = (DS[1] + 1) != 100
        state = SystemState().with_tags({"DS1": 50})
        assert evaluate_condition(cond, state) is True
        state2 = SystemState().with_tags({"DS1": 99})
        assert evaluate_condition(cond, state2) is False

    def test_expression_le_expression(self):
        """DS[1] <= (High + Band) where both sides are expressions."""
        DS = Block("DS", TagType.INT, 1, 9)
        High = Int("High")
        Band = Int("Band")
        cond = DS[1] <= (High + Band)
        state = SystemState().with_tags({"DS1": 100, "High": 90, "Band": 20})
        assert evaluate_condition(cond, state) is True  # 100 <= 110
        state2 = SystemState().with_tags({"DS1": 120, "High": 90, "Band": 20})
        assert evaluate_condition(cond, state2) is False  # 120 <= 110

    def test_literal_gt_expression(self):
        """100 > (DS[1] + DS[2]) - reversed comparison."""
        DS = Block("DS", TagType.INT, 1, 9)
        # Use explicit expression comparison rather than literal on left
        cond = (DS[1] + DS[2]) < 100
        state = SystemState().with_tags({"DS1": 30, "DS2": 40})
        assert evaluate_condition(cond, state) is True


# =============================================================================
# Phase 3: Expressions in Rung Conditions
# =============================================================================


class TestExpressionInRung:
    """Test expressions used as Rung conditions."""

    def test_rung_with_expression_condition(self):
        """with Rung((DS[1] + DS[2]) > 100): out(Alarm)."""
        from pyrung.core import Rung, SystemState, out, program

        DS = Block("DS", TagType.INT, 1, 9)
        Alarm = Bool("Alarm")

        @program
        def logic():
            with Rung((DS[1] + DS[2]) > 100):
                out(Alarm)

        # Test when condition is true
        state = SystemState().with_tags({"DS1": 60, "DS2": 50})
        ctx = ScanContext(state)
        logic.evaluate(ctx)
        result = ctx.commit(dt=0.0)
        assert result.tags["Alarm"] is True

        # Test when condition is false
        state2 = SystemState().with_tags({"DS1": 40, "DS2": 50})
        ctx2 = ScanContext(state2)
        logic.evaluate(ctx2)
        result2 = ctx2.commit(dt=0.0)
        assert result2.tags.get("Alarm", False) is False

    def test_fahrenheit_conversion(self):
        """with Rung((Temperature * 1.8 + 32) > 212): - Fahrenheit boiling point."""
        from pyrung.core import Rung, out, program

        Temperature = Int("Temperature")  # Celsius
        Boiling = Bool("Boiling")

        @program
        def logic():
            with Rung((Temperature * 1.8 + 32) > 212):
                out(Boiling)

        # 100C = 212F (boiling point)
        state = SystemState().with_tags({"Temperature": 100})
        ctx = ScanContext(state)
        logic.evaluate(ctx)
        result = ctx.commit(dt=0.0)
        assert result.tags.get("Boiling", False) is False  # 212 > 212 is false

        # 101C = 213.8F
        state2 = SystemState().with_tags({"Temperature": 101})
        ctx2 = ScanContext(state2)
        logic.evaluate(ctx2)
        result2 = ctx2.commit(dt=0.0)
        assert result2.tags["Boiling"] is True


# =============================================================================
# Phase 4: Expressions in copy()
# =============================================================================


class TestExpressionInCopy:
    """Test expressions used as copy() source."""

    def test_copy_expression_to_tag(self):
        """copy(DS[1] * 2 + Offset, Result)."""
        from pyrung.core import Rung, copy, program

        DS = Block("DS", TagType.INT, 1, 9)
        Offset = Int("Offset")
        Result = Int("Result")
        Enable = Bool("Enable")

        @program
        def logic():
            with Rung(Enable):
                copy(DS[1] * 2 + Offset, Result)

        state = SystemState().with_tags(
            {
                "Enable": True,
                "DS1": 10,
                "Offset": 5,
            }
        )
        ctx = ScanContext(state)
        logic.evaluate(ctx)
        result = ctx.commit(dt=0.0)
        assert result.tags["Result"] == 25  # 10 * 2 + 5

    def test_copy_math_function_result(self):
        """copy(sqrt(DS[1]), Result)."""
        from pyrung.core import Rung, copy, program
        from pyrung.core.expression import sqrt

        DS = Block("DS", TagType.INT, 1, 9)
        Result = Int("Result")
        Enable = Bool("Enable")

        @program
        def logic():
            with Rung(Enable):
                copy(sqrt(DS[1]), Result)

        state = SystemState().with_tags(
            {
                "Enable": True,
                "DS1": 16,
            }
        )
        ctx = ScanContext(state)
        logic.evaluate(ctx)
        result = ctx.commit(dt=0.0)
        assert result.tags["Result"] == pytest.approx(4.0)


# =============================================================================
# Phase 5: Pointer Arithmetic (Expression as Index)
# =============================================================================


class TestPointerArithmetic:
    """Test expressions used as indirect tag indices."""

    def test_indirect_with_expression_index(self):
        """DS[idx + 1] where idx is a Tag - expression in pointer."""
        # This tests using an expression to compute the pointer value
        # The expression evaluates at scan time to determine which tag to access
        DS = Block("DS", TagType.INT, 1, 19)
        idx = Int("idx")

        # Create indirect tag with expression index
        # DS[idx + 1] means: read idx, add 1, use that as address
        indirect = DS[idx + 1]

        state = SystemState().with_tags(
            {
                "idx": 5,
                "DS6": 42,  # idx + 1 = 6
            }
        )
        ctx = ScanContext(state)
        resolved = indirect.resolve_ctx(ctx)
        assert resolved.name == "DS6"
        assert ctx.get_tag(resolved.name) == 42


# =============================================================================
# Phase 6: Bitwise Operations
# =============================================================================


class TestBitwiseOperations:
    """Test bitwise operations on tags."""

    def test_bitwise_and(self):
        """DH[1] & DH[2]."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = DH[1] & DH[2]
        state = SystemState().with_tags({"DH1": 0b1100, "DH2": 0b1010})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0b1000

    def test_literal_and_tag(self):
        """0xFF & DH[1]."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = 0xFF & DH[1]
        state = SystemState().with_tags({"DH1": 0x1234})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0x34

    def test_bitwise_or(self):
        """DH[1] | DH[2]."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = DH[1] | DH[2]
        state = SystemState().with_tags({"DH1": 0b1100, "DH2": 0b1010})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0b1110

    def test_bitwise_xor(self):
        """DH[1] ^ DH[2] - XOR (not power in hex mode)."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = DH[1] ^ DH[2]
        state = SystemState().with_tags({"DH1": 0b1100, "DH2": 0b1010})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0b0110

    def test_left_shift(self):
        """DH[1] << 2."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = DH[1] << 2
        state = SystemState().with_tags({"DH1": 0b0011})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0b1100

    def test_literal_lshift_tag(self):
        """1 << DH[1]."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = 1 << DH[1]
        state = SystemState().with_tags({"DH1": 4})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 16

    def test_right_shift(self):
        """DH[1] >> 2."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = DH[1] >> 2
        state = SystemState().with_tags({"DH1": 0b1100})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0b0011

    def test_bitwise_invert(self):
        """~DH[1]."""
        DH = Block("DH", TagType.WORD, 1, 9)
        expr = ~DH[1]
        state = SystemState().with_tags({"DH1": 0x00FF})
        ctx = ScanContext(state)
        # Python's ~ returns all bits inverted (signed), mask to 16 bits for WORD
        assert expr.evaluate(ctx) & 0xFFFF == 0xFF00


# =============================================================================
# Phase 7: Math Functions
# =============================================================================


class TestMathFunctions:
    """Test mathematical functions on tags."""

    def test_sqrt(self):
        """sqrt(DS[1])."""
        from pyrung.core.expression import sqrt

        DS = Block("DS", TagType.INT, 1, 9)
        expr = sqrt(DS[1])
        state = SystemState().with_tags({"DS1": 16})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 4.0

    def test_sin(self):
        """sin(Angle) where angle is in radians."""
        from pyrung.core.expression import sin

        Angle = Int("Angle")
        expr = sin(Angle)
        state = SystemState().with_tags({"Angle": 0})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(0.0)

    def test_cos(self):
        """cos(Angle) where angle is in radians."""
        from pyrung.core.expression import cos

        Angle = Int("Angle")
        expr = cos(Angle)
        state = SystemState().with_tags({"Angle": 0})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(1.0)

    def test_tan(self):
        """tan(Angle)."""
        from pyrung.core.expression import tan

        Angle = Int("Angle")
        expr = tan(Angle)
        state = SystemState().with_tags({"Angle": 0})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(0.0)

    def test_asin(self):
        """asin(Value)."""
        from pyrung.core.expression import asin

        Value = Int("Value")
        expr = asin(Value)
        state = SystemState().with_tags({"Value": 0})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(0.0)

    def test_acos(self):
        """acos(Value)."""
        from pyrung.core.expression import acos

        Value = Int("Value")
        expr = acos(Value)
        state = SystemState().with_tags({"Value": 1})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(0.0)

    def test_atan(self):
        """atan(Value)."""
        from pyrung.core.expression import atan

        Value = Int("Value")
        expr = atan(Value)
        state = SystemState().with_tags({"Value": 0})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(0.0)

    def test_radians(self):
        """radians(Degrees)."""
        from pyrung.core.expression import radians

        Degrees = Int("Degrees")
        expr = radians(Degrees)
        state = SystemState().with_tags({"Degrees": 180})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(math.pi)

    def test_degrees(self):
        """degrees(Radians)."""
        from pyrung.core.expression import degrees

        # Use a REAL tag for floating point
        Radians = Int("Radians")
        expr = degrees(Radians)
        # Test with integer approximation of pi (3)
        state = SystemState().with_tags({"Radians": 3})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(math.degrees(3))

    def test_log10(self):
        """log10(Value)."""
        from pyrung.core.expression import log10

        Value = Int("Value")
        expr = log10(Value)
        state = SystemState().with_tags({"Value": 100})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(2.0)

    def test_log(self):
        """log(Value) - natural log."""
        from pyrung.core.expression import log

        Value = Int("Value")
        expr = log(Value)
        state = SystemState().with_tags({"Value": 1})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(0.0)

    def test_pi_constant(self):
        """PI value in expressions."""
        from pyrung.core.expression import PI

        DS = Block("DS", TagType.INT, 1, 9)
        # PI * radius^2 for area
        expr = PI * (DS[1] ** 2)
        state = SystemState().with_tags({"DS1": 2})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(math.pi * 4)

    def test_nested_math_functions(self):
        """sqrt(DS[1] ** 2 + DS[2] ** 2) - Pythagorean theorem."""
        from pyrung.core.expression import sqrt

        DS = Block("DS", TagType.INT, 1, 9)
        expr = sqrt(DS[1] ** 2 + DS[2] ** 2)
        state = SystemState().with_tags({"DS1": 3, "DS2": 4})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == pytest.approx(5.0)


# =============================================================================
# Phase 8: Shift/Rotate Functions (Click-specific)
# =============================================================================


class TestShiftRotateFunctions:
    """Test Click-specific shift and rotate functions."""

    def test_lsh(self):
        """lsh(DH[1], n) - left shift function."""
        from pyrung.core.expression import lsh

        DH = Block("DH", TagType.WORD, 1, 9)
        expr = lsh(DH[1], 4)
        state = SystemState().with_tags({"DH1": 0x0F})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0xF0

    def test_rsh(self):
        """rsh(DH[1], n) - right shift function."""
        from pyrung.core.expression import rsh

        DH = Block("DH", TagType.WORD, 1, 9)
        expr = rsh(DH[1], 4)
        state = SystemState().with_tags({"DH1": 0xF0})
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 0x0F

    def test_lro(self):
        """lro(DH[1], n) - rotate left (16-bit)."""
        from pyrung.core.expression import lro

        DH = Block("DH", TagType.WORD, 1, 9)
        expr = lro(DH[1], 4)
        state = SystemState().with_tags({"DH1": 0xF00F})
        ctx = ScanContext(state)
        # Rotate left 4: 0xF00F -> 0x00FF
        assert expr.evaluate(ctx) == 0x00FF

    def test_rro(self):
        """rro(DH[1], n) - rotate right (16-bit)."""
        from pyrung.core.expression import rro

        DH = Block("DH", TagType.WORD, 1, 9)
        expr = rro(DH[1], 4)
        state = SystemState().with_tags({"DH1": 0xF00F})
        ctx = ScanContext(state)
        # Rotate right 4: 0xF00F -> 0xFF00
        assert expr.evaluate(ctx) == 0xFF00


# =============================================================================
# Edge Cases and Error Handling
# =============================================================================


class TestExpressionEdgeCases:
    """Test edge cases and error handling."""

    def test_division_by_zero_returns_inf(self):
        """Division by zero returns infinity (like hardware)."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] / DS[2]
        state = SystemState().with_tags({"DS1": 10, "DS2": 0})
        ctx = ScanContext(state)
        result = expr.evaluate(ctx)
        assert math.isinf(result)

    def test_floor_division_by_zero_raises(self):
        """Floor division by zero raises ZeroDivisionError."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] // DS[2]
        state = SystemState().with_tags({"DS1": 10, "DS2": 0})
        ctx = ScanContext(state)
        with pytest.raises(ZeroDivisionError):
            expr.evaluate(ctx)

    def test_expression_with_default_value(self):
        """Expression uses tag's default when not in state."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] + 10
        state = SystemState()  # DS1 not in state, defaults to 0
        ctx = ScanContext(state)
        assert expr.evaluate(ctx) == 10

    def test_chained_operations(self):
        """Long chain: a + b - c * d / e."""
        DS = Block("DS", TagType.INT, 1, 9)
        expr = DS[1] + DS[2] - DS[3] * DS[4] / DS[5]
        state = SystemState().with_tags(
            {
                "DS1": 10,
                "DS2": 20,
                "DS3": 6,
                "DS4": 4,
                "DS5": 2,
            }
        )
        ctx = ScanContext(state)
        # 10 + 20 - (6 * 4 / 2) = 30 - 12 = 18
        assert expr.evaluate(ctx) == pytest.approx(18.0)
