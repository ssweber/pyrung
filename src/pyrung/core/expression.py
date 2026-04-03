"""Math expression support for pyrung engine.

Enables native Python expressions with Tags in conditions and instruction arguments:
    with Rung((DS[1] + DS[2]) > 100):     # Expression in condition
    copy(DS[1] * 2 + Offset, Result)      # Expression in copy source
"""

from __future__ import annotations

import math
import operator
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from pyrung.core._source import _capture_source

if TYPE_CHECKING:
    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.memory_block import BlockRange
    from pyrung.core.tag import Tag

Numeric = int | float


# =============================================================================
# Operator functions
# =============================================================================


def _op_div(a: Numeric, b: Numeric) -> Numeric:
    if b == 0:
        return float("inf") if a >= 0 else float("-inf")
    return a / b


def _op_and(a: Numeric, b: Numeric) -> int:
    return int(a) & int(b)


def _op_or(a: Numeric, b: Numeric) -> int:
    return int(a) | int(b)


def _op_xor(a: Numeric, b: Numeric) -> int:
    return int(a) ^ int(b)


def _op_lshift(a: Numeric, b: Numeric) -> int:
    return int(a) << int(b)


def _op_rshift(a: Numeric, b: Numeric) -> int:
    return int(a) >> int(b)


def _op_invert(a: Numeric) -> int:
    return ~int(a)


# =============================================================================
# Base Expression Class
# =============================================================================


class Expression(ABC):
    """Base class for all mathematical expressions.

    Expressions are lazy-evaluated at scan time against a ScanContext.
    They can be composed using arithmetic operators and compared to
    produce Conditions.
    """

    @abstractmethod
    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        """Evaluate this expression against a ScanContext.

        Args:
            ctx: ScanContext for resolving tag values.

        Returns:
            The numeric result of the expression.
        """
        pass

    # =========================================================================
    # Arithmetic Operators -> Expression
    # =========================================================================

    def __add__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), operator.add, "+")

    def __radd__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), operator.add, "+")

    def __sub__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), operator.sub, "-")

    def __rsub__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), operator.sub, "-")

    def __mul__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), operator.mul, "*")

    def __rmul__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), operator.mul, "*")

    def __truediv__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), _op_div, "/")

    def __rtruediv__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_div, "/")

    def __floordiv__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), operator.floordiv, "//")

    def __rfloordiv__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), operator.floordiv, "//")

    def __mod__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), operator.mod, "%")

    def __rmod__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), operator.mod, "%")

    def __pow__(self, other: Expression | Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), operator.pow, "**")

    def __rpow__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), operator.pow, "**")

    def __neg__(self) -> UnaryExpr:
        return UnaryExpr(_wrap(self), operator.neg, "-")

    def __pos__(self) -> UnaryExpr:
        return UnaryExpr(_wrap(self), operator.pos, "+")

    def __abs__(self) -> UnaryExpr:
        return UnaryExpr(_wrap(self), abs, "abs")

    # =========================================================================
    # Bitwise Operators -> Expression
    # =========================================================================

    def __and__(self, other: Expression | int) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), _op_and, "&")

    def __rand__(self, other: int) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_and, "&")

    def __or__(self, other: Expression | int) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), _op_or, "|")

    def __ror__(self, other: int) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_or, "|")

    def __xor__(self, other: Expression | int) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), _op_xor, "^")

    def __rxor__(self, other: int) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_xor, "^")

    def __lshift__(self, other: Expression | int) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), _op_lshift, "<<")

    def __rlshift__(self, other: int) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_lshift, "<<")

    def __rshift__(self, other: Expression | int) -> BinaryExpr:
        return BinaryExpr(_wrap(self), _wrap(other), _op_rshift, ">>")

    def __rrshift__(self, other: int) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_rshift, ">>")

    def __invert__(self) -> UnaryExpr:
        return UnaryExpr(_wrap(self), _op_invert, "~")

    # =========================================================================
    # Comparison Operators -> Condition
    # =========================================================================

    def __eq__(self, other: object) -> ExprCompare:  # ty: ignore[invalid-method-override]
        cond = ExprCompare(_wrap(self), _wrap(other), operator.eq, "==")
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __ne__(self, other: object) -> ExprCompare:  # ty: ignore[invalid-method-override]
        cond = ExprCompare(_wrap(self), _wrap(other), operator.ne, "!=")
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __lt__(self, other: Expression | Numeric) -> ExprCompare:
        cond = ExprCompare(_wrap(self), _wrap(other), operator.lt, "<")
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __le__(self, other: Expression | Numeric) -> ExprCompare:
        cond = ExprCompare(_wrap(self), _wrap(other), operator.le, "<=")
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __gt__(self, other: Expression | Numeric) -> ExprCompare:
        cond = ExprCompare(_wrap(self), _wrap(other), operator.gt, ">")
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond

    def __ge__(self, other: Expression | Numeric) -> ExprCompare:
        cond = ExprCompare(_wrap(self), _wrap(other), operator.ge, ">=")
        cond.source_file, cond.source_line = _capture_source(depth=2)
        return cond


# =============================================================================
# Helper to wrap literals
# =============================================================================


def _wrap(value: Any) -> Expression:
    """Wrap a value as an Expression if it isn't already."""
    from pyrung.core.tag import Tag

    if isinstance(value, Expression):
        return value
    if isinstance(value, Tag):
        return TagExpr(value)
    return LiteralExpr(value)


# =============================================================================
# Tag Expression (wraps a Tag for expression operations)
# =============================================================================


class TagExpr(Expression):
    """Expression that reads a Tag's value."""

    def __init__(self, tag: Any):
        from pyrung.core.tag import Tag

        if not isinstance(tag, Tag):
            raise TypeError(f"Expected Tag, got {type(tag)}")
        self.tag = tag

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return ctx.get_tag(self.tag.name, self.tag.default)

    def __repr__(self) -> str:
        return f"TagExpr({self.tag.name})"


# =============================================================================
# Literal Expression
# =============================================================================


class LiteralExpr(Expression):
    """Expression that holds a constant value."""

    def __init__(self, value: Numeric):
        self.value = value

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return self.value

    def __repr__(self) -> str:
        return f"LiteralExpr({self.value})"


# =============================================================================
# Binary Expression (replaces Add/Sub/Mul/Div/FloorDiv/Mod/Pow/And/Or/Xor/LShift/RShift)
# =============================================================================


class BinaryExpr(Expression):
    """Binary operation: left <op> right."""

    def __init__(self, left: Expression, right: Expression, op: Any, symbol: str):
        self.left = left
        self.right = right
        self.op = op
        self.symbol = symbol

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return self.op(self.left.evaluate(ctx), self.right.evaluate(ctx))

    def __repr__(self) -> str:
        return f"({self.left} {self.symbol} {self.right})"


# =============================================================================
# Unary Expression (replaces Neg/Pos/Abs/Invert)
# =============================================================================


class UnaryExpr(Expression):
    """Unary operation: <op>(operand)."""

    def __init__(self, operand: Expression, op: Any, symbol: str):
        self.operand = operand
        self.op = op
        self.symbol = symbol

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return self.op(self.operand.evaluate(ctx))

    def __repr__(self) -> str:
        if self.symbol == "abs":
            return f"abs({self.operand})"
        return f"({self.symbol}{self.operand})"


# =============================================================================
# Expression Comparison Condition (replaces ExprCompareEq/Ne/Lt/Le/Gt/Ge)
# =============================================================================


from pyrung.core.condition import Condition


class ExprCompare(Condition):
    """Comparison condition for expressions: left <op> right."""

    def __init__(self, left: Expression, right: Expression, op: Any, symbol: str):
        self.left = left
        self.right = right
        self.op = op
        self.symbol = symbol

    def evaluate(self, ctx: ScanContext | ConditionView) -> bool:
        return self.op(self.left.evaluate(ctx), self.right.evaluate(ctx))


# =============================================================================
# Math Functions
# =============================================================================


class MathFuncExpr(Expression):
    """Base class for single-argument math function expressions."""

    def __init__(self, operand: Expression, func: Any, name: str):
        self.operand = operand
        self.func = func
        self.name = name

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return self.func(self.operand.evaluate(ctx))

    def __repr__(self) -> str:
        return f"{self.name}({self.operand})"


def sqrt(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Square root function."""
    return MathFuncExpr(_wrap(x), math.sqrt, "sqrt")


def sin(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Sine function (radians)."""
    return MathFuncExpr(_wrap(x), math.sin, "sin")


def cos(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Cosine function (radians)."""
    return MathFuncExpr(_wrap(x), math.cos, "cos")


def tan(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Tangent function (radians)."""
    return MathFuncExpr(_wrap(x), math.tan, "tan")


def asin(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Arc sine function (returns radians)."""
    return MathFuncExpr(_wrap(x), math.asin, "asin")


def acos(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Arc cosine function (returns radians)."""
    return MathFuncExpr(_wrap(x), math.acos, "acos")


def atan(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Arc tangent function (returns radians)."""
    return MathFuncExpr(_wrap(x), math.atan, "atan")


def radians(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Convert degrees to radians."""
    return MathFuncExpr(_wrap(x), math.radians, "radians")


def degrees(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Convert radians to degrees."""
    return MathFuncExpr(_wrap(x), math.degrees, "degrees")


def log10(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Base-10 logarithm."""
    return MathFuncExpr(_wrap(x), math.log10, "log10")


def log(x: Expression | Numeric | Tag) -> MathFuncExpr:
    """Natural logarithm."""
    return MathFuncExpr(_wrap(x), math.log, "log")


# PI constant as an expression
PI = LiteralExpr(math.pi)


# =============================================================================
# Shift/Rotate Functions (Click-specific)
# =============================================================================


class ShiftFuncExpr(Expression):
    """Base class for shift/rotate function expressions."""

    def __init__(self, value: Expression, count: Expression, func: Any, name: str):
        self.value = value
        self.count = count
        self.func = func
        self.name = name

    def evaluate(self, ctx: ScanContext | ConditionView) -> int:
        val = int(self.value.evaluate(ctx))
        cnt = int(self.count.evaluate(ctx))
        return self.func(val, cnt)

    def __repr__(self) -> str:
        return f"{self.name}({self.value}, {self.count})"


def lsh(x: Expression | int | Tag, n: Expression | int | Tag) -> ShiftFuncExpr:
    """Left shift function: lsh(value, count)."""
    return ShiftFuncExpr(_wrap(x), _wrap(n), lambda v, c: v << c, "lsh")


def rsh(x: Expression | int | Tag, n: Expression | int | Tag) -> ShiftFuncExpr:
    """Right shift function: rsh(value, count)."""
    return ShiftFuncExpr(_wrap(x), _wrap(n), lambda v, c: v >> c, "rsh")


def _rotate_left_16(value: int, count: int) -> int:
    """Rotate left on 16-bit value."""
    count = count % 16
    value = value & 0xFFFF
    return ((value << count) | (value >> (16 - count))) & 0xFFFF


def _rotate_right_16(value: int, count: int) -> int:
    """Rotate right on 16-bit value."""
    count = count % 16
    value = value & 0xFFFF
    return ((value >> count) | (value << (16 - count))) & 0xFFFF


def lro(x: Expression | int | Tag, n: Expression | int | Tag) -> ShiftFuncExpr:
    """Rotate left function (16-bit): lro(value, count)."""
    return ShiftFuncExpr(_wrap(x), _wrap(n), _rotate_left_16, "lro")


def rro(x: Expression | int | Tag, n: Expression | int | Tag) -> ShiftFuncExpr:
    """Rotate right function (16-bit): rro(value, count)."""
    return ShiftFuncExpr(_wrap(x), _wrap(n), _rotate_right_16, "rro")


# =============================================================================
# Aggregate Functions
# =============================================================================


class SumExpr(Expression):
    """Sum of all tag values in a block range."""

    def __init__(self, block_range: BlockRange) -> None:
        self.block_range = block_range

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return sum(ctx.get_tag(tag.name, 0) for tag in self.block_range)

    def __repr__(self) -> str:
        return f"sum({self.block_range!r})"


# =============================================================================
# Expression Formatting (DSL-friendly text)
# =============================================================================


def format_expr(expr: Expression) -> str:
    """Convert an Expression tree to DSL-friendly text.

    Examples:
        TagExpr(Tag("DS1")) → "DS1"
        LiteralExpr(42) → "42"
        BinaryExpr(+) → "A + B"
        MathFuncExpr sqrt(X) → "sqrt(X)"
        ShiftFuncExpr lsh(A, 3) → "lsh(A, 3)"
    """
    # Leaf nodes
    if isinstance(expr, TagExpr):
        return expr.tag.name
    if isinstance(expr, LiteralExpr):
        return repr(expr.value)
    # SumExpr — aggregate over block range
    if isinstance(expr, SumExpr):
        return f"sum({expr.block_range!r})"
    # ShiftFuncExpr — two-argument shift/rotate functions (before MathFuncExpr)
    if isinstance(expr, ShiftFuncExpr):
        return f"{expr.name}({format_expr(expr.value)}, {format_expr(expr.count)})"
    # MathFuncExpr — single-argument math functions
    if isinstance(expr, MathFuncExpr):
        return f"{expr.name}({format_expr(expr.operand)})"
    # Unary operations
    if isinstance(expr, UnaryExpr):
        inner = format_expr(expr.operand)
        if expr.symbol == "abs":
            return f"abs({inner})"
        if isinstance(expr.operand, BinaryExpr):
            return f"{expr.symbol}({inner})"
        return f"{expr.symbol}{inner}"
    # Binary operations
    if isinstance(expr, BinaryExpr):
        left_str = format_expr(expr.left)
        right_str = format_expr(expr.right)
        if isinstance(expr.left, BinaryExpr):
            left_str = f"({left_str})"
        if isinstance(expr.right, BinaryExpr):
            right_str = f"({right_str})"
        return f"{left_str} {expr.symbol} {right_str}"
    # Unknown expression type — fallback
    return repr(expr)
