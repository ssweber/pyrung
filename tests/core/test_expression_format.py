"""Tests for format_expr() — DSL-friendly expression formatting."""

from pyrung.core.expression import (
    AbsExpr,
    AddExpr,
    AndExpr,
    DivExpr,
    FloorDivExpr,
    InvertExpr,
    LiteralExpr,
    LShiftExpr,
    ModExpr,
    MulExpr,
    NegExpr,
    OrExpr,
    PosExpr,
    PowExpr,
    RShiftExpr,
    SubExpr,
    TagExpr,
    XorExpr,
    format_expr,
    lro,
    lsh,
    rro,
    rsh,
    sqrt,
)
from pyrung.core.tag import Tag, TagType


def _tag(name: str) -> Tag:
    return Tag(name, TagType.INT)


class TestLeafNodes:
    def test_tag_expr(self):
        t = _tag("DS1")
        assert format_expr(TagExpr(t)) == "DS1"

    def test_literal_int(self):
        assert format_expr(LiteralExpr(42)) == "42"

    def test_literal_float(self):
        assert format_expr(LiteralExpr(3.14)) == "3.14"

    def test_literal_zero(self):
        assert format_expr(LiteralExpr(0)) == "0"


class TestBinaryArithmetic:
    def test_add(self):
        expr = AddExpr(TagExpr(_tag("A")), LiteralExpr(1))
        assert format_expr(expr) == "A + 1"

    def test_sub(self):
        expr = SubExpr(TagExpr(_tag("A")), TagExpr(_tag("B")))
        assert format_expr(expr) == "A - B"

    def test_mul(self):
        expr = MulExpr(TagExpr(_tag("X")), LiteralExpr(2))
        assert format_expr(expr) == "X * 2"

    def test_div(self):
        expr = DivExpr(TagExpr(_tag("X")), LiteralExpr(3))
        assert format_expr(expr) == "X / 3"

    def test_floordiv(self):
        expr = FloorDivExpr(TagExpr(_tag("X")), LiteralExpr(3))
        assert format_expr(expr) == "X // 3"

    def test_mod(self):
        expr = ModExpr(TagExpr(_tag("X")), LiteralExpr(5))
        assert format_expr(expr) == "X % 5"

    def test_pow(self):
        expr = PowExpr(TagExpr(_tag("X")), LiteralExpr(2))
        assert format_expr(expr) == "X ** 2"


class TestBinaryBitwise:
    def test_and(self):
        expr = AndExpr(TagExpr(_tag("A")), LiteralExpr(0xFF))
        assert format_expr(expr) == "A & 255"

    def test_or(self):
        expr = OrExpr(TagExpr(_tag("A")), TagExpr(_tag("B")))
        assert format_expr(expr) == "A | B"

    def test_xor(self):
        expr = XorExpr(TagExpr(_tag("A")), TagExpr(_tag("B")))
        assert format_expr(expr) == "A ^ B"

    def test_lshift(self):
        expr = LShiftExpr(TagExpr(_tag("A")), LiteralExpr(4))
        assert format_expr(expr) == "A << 4"

    def test_rshift(self):
        expr = RShiftExpr(TagExpr(_tag("A")), LiteralExpr(2))
        assert format_expr(expr) == "A >> 2"


class TestNestedParens:
    def test_mul_of_add(self):
        inner = AddExpr(TagExpr(_tag("A")), TagExpr(_tag("B")))
        expr = MulExpr(inner, LiteralExpr(2))
        assert format_expr(expr) == "(A + B) * 2"

    def test_add_of_mul(self):
        inner = MulExpr(TagExpr(_tag("A")), LiteralExpr(2))
        expr = AddExpr(inner, TagExpr(_tag("B")))
        assert format_expr(expr) == "(A * 2) + B"

    def test_nested_both_sides(self):
        left = AddExpr(TagExpr(_tag("A")), LiteralExpr(1))
        right = SubExpr(TagExpr(_tag("B")), LiteralExpr(2))
        expr = MulExpr(left, right)
        assert format_expr(expr) == "(A + 1) * (B - 2)"

    def test_leaf_children_no_parens(self):
        expr = AddExpr(TagExpr(_tag("X")), LiteralExpr(1))
        assert format_expr(expr) == "X + 1"


class TestUnary:
    def test_neg(self):
        expr = NegExpr(TagExpr(_tag("X")))
        assert format_expr(expr) == "-X"

    def test_pos(self):
        expr = PosExpr(TagExpr(_tag("X")))
        assert format_expr(expr) == "+X"

    def test_invert(self):
        expr = InvertExpr(TagExpr(_tag("X")))
        assert format_expr(expr) == "~X"

    def test_neg_of_binary(self):
        inner = AddExpr(TagExpr(_tag("A")), TagExpr(_tag("B")))
        expr = NegExpr(inner)
        assert format_expr(expr) == "-(A + B)"

    def test_abs(self):
        expr = AbsExpr(TagExpr(_tag("X")))
        assert format_expr(expr) == "abs(X)"


class TestMathFunctions:
    def test_sqrt(self):
        expr = sqrt(_tag("X"))
        assert format_expr(expr) == "sqrt(X)"

    def test_sqrt_of_expression(self):
        inner = AddExpr(TagExpr(_tag("A")), LiteralExpr(1))
        import math

        from pyrung.core.expression import MathFuncExpr

        expr = MathFuncExpr(inner, math.sqrt, "sqrt")
        assert format_expr(expr) == "sqrt(A + 1)"


class TestShiftFunctions:
    def test_lsh(self):
        expr = lsh(_tag("A"), 3)
        assert format_expr(expr) == "lsh(A, 3)"

    def test_rsh(self):
        expr = rsh(_tag("A"), 2)
        assert format_expr(expr) == "rsh(A, 2)"

    def test_lro(self):
        expr = lro(_tag("A"), 4)
        assert format_expr(expr) == "lro(A, 4)"

    def test_rro(self):
        expr = rro(_tag("A"), 1)
        assert format_expr(expr) == "rro(A, 1)"


class TestUnknownFallback:
    def test_unknown_expression_type(self):
        from pyrung.core.expression import Expression

        class CustomExpr(Expression):
            def evaluate(self, ctx):
                return 0

        expr = CustomExpr()
        result = format_expr(expr)
        # Should not raise; falls back to repr
        assert isinstance(result, str)
