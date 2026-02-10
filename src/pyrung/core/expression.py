"""Math expression support for pyrung engine.

Enables native Python expressions with Tags in conditions and instruction arguments:
    with Rung((DS[1] + DS[2]) > 100):     # Expression in condition
    copy(DS[1] * 2 + Offset, Result)      # Expression in copy source
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pyrung.core.context import ScanContext
    from pyrung.core.tag import Tag

Numeric = int | float


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
    def evaluate(self, ctx: ScanContext) -> Numeric:
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

    def __add__(self, other: Expression | Numeric) -> AddExpr:
        return AddExpr(self, _wrap(other))

    def __radd__(self, other: Numeric) -> AddExpr:
        return AddExpr(_wrap(other), self)

    def __sub__(self, other: Expression | Numeric) -> SubExpr:
        return SubExpr(self, _wrap(other))

    def __rsub__(self, other: Numeric) -> SubExpr:
        return SubExpr(_wrap(other), self)

    def __mul__(self, other: Expression | Numeric) -> MulExpr:
        return MulExpr(self, _wrap(other))

    def __rmul__(self, other: Numeric) -> MulExpr:
        return MulExpr(_wrap(other), self)

    def __truediv__(self, other: Expression | Numeric) -> DivExpr:
        return DivExpr(self, _wrap(other))

    def __rtruediv__(self, other: Numeric) -> DivExpr:
        return DivExpr(_wrap(other), self)

    def __floordiv__(self, other: Expression | Numeric) -> FloorDivExpr:
        return FloorDivExpr(self, _wrap(other))

    def __rfloordiv__(self, other: Numeric) -> FloorDivExpr:
        return FloorDivExpr(_wrap(other), self)

    def __mod__(self, other: Expression | Numeric) -> ModExpr:
        return ModExpr(self, _wrap(other))

    def __rmod__(self, other: Numeric) -> ModExpr:
        return ModExpr(_wrap(other), self)

    def __pow__(self, other: Expression | Numeric) -> PowExpr:
        return PowExpr(self, _wrap(other))

    def __rpow__(self, other: Numeric) -> PowExpr:
        return PowExpr(_wrap(other), self)

    def __neg__(self) -> NegExpr:
        return NegExpr(self)

    def __pos__(self) -> PosExpr:
        return PosExpr(self)

    def __abs__(self) -> AbsExpr:
        return AbsExpr(self)

    # =========================================================================
    # Bitwise Operators -> Expression
    # =========================================================================

    def __and__(self, other: Expression | int) -> AndExpr:
        return AndExpr(self, _wrap(other))

    def __rand__(self, other: int) -> AndExpr:
        return AndExpr(_wrap(other), self)

    def __or__(self, other: Expression | int) -> OrExpr:
        return OrExpr(self, _wrap(other))

    def __ror__(self, other: int) -> OrExpr:
        return OrExpr(_wrap(other), self)

    def __xor__(self, other: Expression | int) -> XorExpr:
        return XorExpr(self, _wrap(other))

    def __rxor__(self, other: int) -> XorExpr:
        return XorExpr(_wrap(other), self)

    def __lshift__(self, other: Expression | int) -> LShiftExpr:
        return LShiftExpr(self, _wrap(other))

    def __rlshift__(self, other: int) -> LShiftExpr:
        return LShiftExpr(_wrap(other), self)

    def __rshift__(self, other: Expression | int) -> RShiftExpr:
        return RShiftExpr(self, _wrap(other))

    def __rrshift__(self, other: int) -> RShiftExpr:
        return RShiftExpr(_wrap(other), self)

    def __invert__(self) -> InvertExpr:
        return InvertExpr(self)

    # =========================================================================
    # Comparison Operators -> Condition
    # =========================================================================

    def __eq__(self, other: object) -> ExprCompareEq:  # type: ignore[override]
        return ExprCompareEq(self, _wrap(other))

    def __ne__(self, other: object) -> ExprCompareNe:  # type: ignore[override]
        return ExprCompareNe(self, _wrap(other))

    def __lt__(self, other: Expression | Numeric) -> ExprCompareLt:
        return ExprCompareLt(self, _wrap(other))

    def __le__(self, other: Expression | Numeric) -> ExprCompareLe:
        return ExprCompareLe(self, _wrap(other))

    def __gt__(self, other: Expression | Numeric) -> ExprCompareGt:
        return ExprCompareGt(self, _wrap(other))

    def __ge__(self, other: Expression | Numeric) -> ExprCompareGe:
        return ExprCompareGe(self, _wrap(other))


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

    def evaluate(self, ctx: ScanContext) -> Numeric:
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

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.value

    def __repr__(self) -> str:
        return f"LiteralExpr({self.value})"


# =============================================================================
# Binary Arithmetic Expressions
# =============================================================================


class AddExpr(Expression):
    """Addition expression: left + right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.left.evaluate(ctx) + self.right.evaluate(ctx)

    def __repr__(self) -> str:
        return f"({self.left} + {self.right})"


class SubExpr(Expression):
    """Subtraction expression: left - right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.left.evaluate(ctx) - self.right.evaluate(ctx)

    def __repr__(self) -> str:
        return f"({self.left} - {self.right})"


class MulExpr(Expression):
    """Multiplication expression: left * right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.left.evaluate(ctx) * self.right.evaluate(ctx)

    def __repr__(self) -> str:
        return f"({self.left} * {self.right})"


class DivExpr(Expression):
    """Division expression: left / right (true division, returns float)."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        left_val = self.left.evaluate(ctx)
        right_val = self.right.evaluate(ctx)
        if right_val == 0:
            # Return infinity like hardware typically does
            return float("inf") if left_val >= 0 else float("-inf")
        return left_val / right_val

    def __repr__(self) -> str:
        return f"({self.left} / {self.right})"


class FloorDivExpr(Expression):
    """Floor division expression: left // right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.left.evaluate(ctx) // self.right.evaluate(ctx)

    def __repr__(self) -> str:
        return f"({self.left} // {self.right})"


class ModExpr(Expression):
    """Modulo expression: left % right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.left.evaluate(ctx) % self.right.evaluate(ctx)

    def __repr__(self) -> str:
        return f"({self.left} % {self.right})"


class PowExpr(Expression):
    """Power expression: left ** right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return self.left.evaluate(ctx) ** self.right.evaluate(ctx)

    def __repr__(self) -> str:
        return f"({self.left} ** {self.right})"


# =============================================================================
# Unary Arithmetic Expressions
# =============================================================================


class NegExpr(Expression):
    """Negation expression: -operand."""

    def __init__(self, operand: Expression):
        self.operand = operand

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return -self.operand.evaluate(ctx)

    def __repr__(self) -> str:
        return f"(-{self.operand})"


class PosExpr(Expression):
    """Positive expression: +operand (identity)."""

    def __init__(self, operand: Expression):
        self.operand = operand

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return +self.operand.evaluate(ctx)

    def __repr__(self) -> str:
        return f"(+{self.operand})"


class AbsExpr(Expression):
    """Absolute value expression: abs(operand)."""

    def __init__(self, operand: Expression):
        self.operand = operand

    def evaluate(self, ctx: ScanContext) -> Numeric:
        return abs(self.operand.evaluate(ctx))

    def __repr__(self) -> str:
        return f"abs({self.operand})"


# =============================================================================
# Bitwise Expressions
# =============================================================================


class AndExpr(Expression):
    """Bitwise AND: left & right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> int:
        return int(self.left.evaluate(ctx)) & int(self.right.evaluate(ctx))

    def __repr__(self) -> str:
        return f"({self.left} & {self.right})"


class OrExpr(Expression):
    """Bitwise OR: left | right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> int:
        return int(self.left.evaluate(ctx)) | int(self.right.evaluate(ctx))

    def __repr__(self) -> str:
        return f"({self.left} | {self.right})"


class XorExpr(Expression):
    """Bitwise XOR: left ^ right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> int:
        return int(self.left.evaluate(ctx)) ^ int(self.right.evaluate(ctx))

    def __repr__(self) -> str:
        return f"({self.left} ^ {self.right})"


class LShiftExpr(Expression):
    """Left shift: left << right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> int:
        return int(self.left.evaluate(ctx)) << int(self.right.evaluate(ctx))

    def __repr__(self) -> str:
        return f"({self.left} << {self.right})"


class RShiftExpr(Expression):
    """Right shift: left >> right."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> int:
        return int(self.left.evaluate(ctx)) >> int(self.right.evaluate(ctx))

    def __repr__(self) -> str:
        return f"({self.left} >> {self.right})"


class InvertExpr(Expression):
    """Bitwise invert: ~operand."""

    def __init__(self, operand: Expression):
        self.operand = operand

    def evaluate(self, ctx: ScanContext) -> int:
        return ~int(self.operand.evaluate(ctx))

    def __repr__(self) -> str:
        return f"(~{self.operand})"


# =============================================================================
# Expression Comparison Conditions
# =============================================================================


from pyrung.core.condition import Condition


class ExprCompareEq(Condition):
    """Equality comparison for expressions: expr == value."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> bool:
        return self.left.evaluate(ctx) == self.right.evaluate(ctx)


class ExprCompareNe(Condition):
    """Inequality comparison for expressions: expr != value."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> bool:
        return self.left.evaluate(ctx) != self.right.evaluate(ctx)


class ExprCompareLt(Condition):
    """Less-than comparison for expressions: expr < value."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> bool:
        return self.left.evaluate(ctx) < self.right.evaluate(ctx)


class ExprCompareLe(Condition):
    """Less-than-or-equal comparison for expressions: expr <= value."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> bool:
        return self.left.evaluate(ctx) <= self.right.evaluate(ctx)


class ExprCompareGt(Condition):
    """Greater-than comparison for expressions: expr > value."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> bool:
        return self.left.evaluate(ctx) > self.right.evaluate(ctx)


class ExprCompareGe(Condition):
    """Greater-than-or-equal comparison for expressions: expr >= value."""

    def __init__(self, left: Expression, right: Expression):
        self.left = left
        self.right = right

    def evaluate(self, ctx: ScanContext) -> bool:
        return self.left.evaluate(ctx) >= self.right.evaluate(ctx)


# =============================================================================
# Math Functions
# =============================================================================


class MathFuncExpr(Expression):
    """Base class for single-argument math function expressions."""

    def __init__(self, operand: Expression, func: Any, name: str):
        self.operand = operand
        self.func = func
        self.name = name

    def evaluate(self, ctx: ScanContext) -> Numeric:
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

    def evaluate(self, ctx: ScanContext) -> int:
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
