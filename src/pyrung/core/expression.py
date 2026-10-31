"""Math expression support for pyrung engine.

Enables native Python expressions with Tags in conditions and instruction arguments:
    with Rung((DS[1] + DS[2]) > 100):     # Expression in condition
    copy(DS[1] * 2 + Offset, Result)      # Expression in copy source
"""

from __future__ import annotations

import itertools
import math
import operator
import weakref
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import TYPE_CHECKING, Any

from pyrung.core._source import _capture_source

if TYPE_CHECKING:
    from collections.abc import Iterable

    from pyrung.core.context import ConditionView, ScanContext
    from pyrung.core.memory_block import BlockRange
    from pyrung.core.tag import Tag

Numeric = int | float


# =============================================================================
# Operator functions
# =============================================================================


#: True while ``calc`` evaluates its expression: there a division by zero
#: raises ``ZeroDivisionError`` (CLICK's Math writes 0 and sets SC40), as it
#: does in the compiled kernel.  Elsewhere (a comparison in a rung condition)
#: it reads as +/-Inf.  A REAL can hold +/-Inf itself (a Math overflow), so
#: ``calc`` cannot tell a division by zero from an Inf operand afterwards.
STRICT_DIVISION: ContextVar[bool] = ContextVar("pyrung_strict_division", default=False)


def _op_div(a: Numeric, b: Numeric) -> Numeric:
    if b == 0:
        if STRICT_DIVISION.get():
            raise ZeroDivisionError("division by zero")
        return float("inf") if a >= 0 else float("-inf")
    return a / b


def _op_pow(a: Numeric, b: Numeric) -> Numeric:
    """``a ** b``; a float result past the float range is +/-Inf (IEEE 754).

    Python raises ``OverflowError`` where ``*`` would give Inf, so a ``calc``
    of ``Big ** 2`` would store 0, not the Inf a ``Big * Big`` stores.  The
    sign is the IEEE one: negative only for a negative base and an odd
    integer exponent.
    """
    try:
        return a**b
    except OverflowError:
        if not (isinstance(a, float) or isinstance(b, float)):
            raise  # an int too large for a float: calc stores 0 with SC43
        odd = b % 2 == 1 if isinstance(b, int) else b.is_integer() and int(b) % 2 == 1
        negative = a < 0 and odd
        return -math.inf if negative else math.inf


def bit_operand(value: Numeric) -> int:
    """``int(value)`` for an operand of a bitwise or shift operation.

    An Inf raises ``OverflowError`` in ``int()``, and so does a NaN here
    (``int(nan)`` raises ``ValueError``): ``calc`` stores 0 and sets the
    out-of-range flag for either.  The compiled kernel and generated
    CircuitPython use the ``_bit_int`` helper, which does the same.
    """
    if value != value:  # NaN
        raise OverflowError("cannot convert float NaN to integer")
    return int(value)


def _op_and(a: Numeric, b: Numeric) -> int:
    return bit_operand(a) & bit_operand(b)


def _op_or(a: Numeric, b: Numeric) -> int:
    return bit_operand(a) | bit_operand(b)


def _op_xor(a: Numeric, b: Numeric) -> int:
    return bit_operand(a) ^ bit_operand(b)


def _op_lshift(a: Numeric, b: Numeric) -> int:
    return bit_operand(a) << bit_operand(b)


def _op_rshift(a: Numeric, b: Numeric) -> int:
    return bit_operand(a) >> bit_operand(b)


def _op_invert(a: Numeric) -> int:
    return ~bit_operand(a)


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
        return BinaryExpr(_wrap(self), _wrap(other), _op_pow, "**")

    def __rpow__(self, other: Numeric) -> BinaryExpr:
        return BinaryExpr(_wrap(other), _wrap(self), _op_pow, "**")

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


class MathDomainError(ValueError):
    """A math function was given an operand outside its domain.

    ``sqrt`` of a negative, ``log``/``log10`` of 0 or a negative, ``asin``/``acos``
    outside -1..1.  ``calc`` turns it into CLICK's math-operation error (SC46):
    the destination gets 0, the flag is on for the rest of the scan, and the
    PLC stays in RUN.
    """


#: Math functions with a restricted domain: the ones that can raise MathDomainError.
DOMAIN_LIMITED_MATH_FUNCS = frozenset({"sqrt", "log", "log10", "asin", "acos"})


def math_func_value(func: Any, name: str, value: Numeric) -> Numeric:
    """``func(value)``, IEEE 754 for a +/-Inf or NaN operand.

    A ``ValueError`` from one of :data:`DOMAIN_LIMITED_MATH_FUNCS` is a domain
    error (``sqrt(-1)``, ``asin(Inf)``).  Any other function only raises for a
    non-finite operand (``sin(Inf)``), whose IEEE result is NaN.
    """
    try:
        return func(value)
    except ValueError:
        if name not in DOMAIN_LIMITED_MATH_FUNCS:
            return math.nan
        raise MathDomainError(f"{name}({value!r}): math domain error") from None


class MathFuncExpr(Expression):
    """Base class for single-argument math function expressions."""

    def __init__(self, operand: Expression, func: Any, name: str):
        self.operand = operand
        self.func = func
        self.name = name

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return math_func_value(self.func, self.name, self.operand.evaluate(ctx))

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
        val = bit_operand(self.value.evaluate(ctx))
        cnt = bit_operand(self.count.evaluate(ctx))
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
# Random Draw (CLICK RAND)
# =============================================================================

#: Name prefix of the hidden tag each ``randint`` site reads its draw from.
RANDINT_TAG_PREFIX = "_rand."

#: Stream seed for every draw.  Fixed, so two runs of one program agree.
RANDINT_SEED = 0x5EED

_MASK64 = (1 << 64) - 1
_randint_site_counter = itertools.count(1)
_randint_sites: weakref.WeakValueDictionary[str, RandIntExpr] = weakref.WeakValueDictionary()


def _splitmix64(x: int) -> int:
    x = (x + 0x9E3779B97F4A7C15) & _MASK64
    x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _MASK64
    x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _MASK64
    return x ^ (x >> 31)


def randint_draw(site: int, scan_id: int, lo: int, hi: int) -> int:
    """The draw of *site* on scan *scan_id*: a pure function, so replay, seek
    and fork reproduce it.  ``lo > hi`` draws from ``hi..lo``."""
    if lo > hi:
        lo, hi = hi, lo
    z = _splitmix64(_splitmix64(RANDINT_SEED ^ site) ^ (scan_id & _MASK64))
    return lo + z % (hi - lo + 1)


def _operand_span(operand: Expression) -> tuple[int, int] | None:
    """Lowest and highest integer a ``randint`` bound can take, if declared."""
    if isinstance(operand, LiteralExpr):
        return int(operand.value), int(operand.value)
    if isinstance(operand, TagExpr):
        tag = operand.tag
        if tag.choices:
            keys = [int(k) for k in tag.choices]
            return min(keys), max(keys)
        if tag.min is not None and tag.max is not None:
            return math.ceil(tag.min), math.floor(tag.max)
    return None


class RandIntExpr(Expression):
    """``randint(lo, hi)``: an integer drawn each scan, ``lo..hi`` inclusive.

    The draw is an input, not a computation.  Each site owns a hidden external
    tag (``_rand.<n>``) that the scan's input drain writes after patches, so a
    ``patch()`` of that tag overrides the draw for one scan and a ``force()``
    pins it.  Logic reads the tag; the prover sees an external input over
    ``lo..hi`` (inclusive), or over the hull of a bound tag's declared
    ``min``/``max``/``choices``.  Bounds that are tags are read at the start of
    the scan.

    A sibling of the other expression classes, never a subclass: no
    ``LiteralExpr`` / ``MathFuncExpr`` arm may treat a draw as a pure function
    of its operands.
    """

    def __init__(self, lo: Expression, hi: Expression) -> None:
        from pyrung.core.tag import Tag, TagType

        self.lo = lo
        self.hi = hi
        self.site = next(_randint_site_counter)
        # The draw's range: the bounds themselves when literal, else the hull of
        # what each bound tag declares (a draw never leaves min(lo, hi)..max(lo, hi)).
        lo_span = _operand_span(lo)
        hi_span = _operand_span(hi)
        bounds: dict[str, Any] = {}
        if lo_span is not None and hi_span is not None:
            low = min(lo_span[0], hi_span[0])
            high = max(lo_span[1], hi_span[1])
            bounds["default"] = low
            if low < high:
                bounds["min"], bounds["max"] = low, high
        self.tag = Tag(
            f"{RANDINT_TAG_PREFIX}{self.site}",
            TagType.DINT,
            external=True,
            comment="randint draw",
            **bounds,
        )
        _randint_sites[self.tag.name] = self

    def draw(self, ctx: ScanContext) -> int:
        """This scan's draw, from the bounds as the scan starts."""
        return randint_draw(
            self.site, ctx.scan_id, int(self.lo.evaluate(ctx)), int(self.hi.evaluate(ctx))
        )

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return ctx.get_tag(self.tag.name, self.tag.default)

    def __repr__(self) -> str:
        return f"randint({format_expr(self.lo)}, {format_expr(self.hi)})"


def randint(lo: Expression | int | Tag, hi: Expression | int | Tag) -> RandIntExpr:
    """Random integer from ``lo`` to ``hi`` inclusive, drawn once per scan (CLICK ``RAND``).

    Use it inside ``calc()``.  Draws are deterministic per scan, so replay, seek
    and fork see the same values; ``patch()`` or ``force()`` the site's tag
    (``expr.tag``) to choose a draw in a test.
    """
    return RandIntExpr(_wrap(lo), _wrap(hi))


def is_randint_tag(name: str) -> bool:
    """Whether *name* is a ``randint`` site's hidden draw tag."""
    return name.startswith(RANDINT_TAG_PREFIX)


def randint_sites(tag_names: Iterable[str]) -> tuple[RandIntExpr, ...]:
    """The live ``randint`` sites among *tag_names*, in site order."""
    sites = [_randint_sites.get(name) for name in tag_names if is_randint_tag(name)]
    return tuple(sorted((s for s in sites if s is not None), key=lambda s: s.site))


# =============================================================================
# Aggregate Functions
# =============================================================================


def _range_dsl(block_range: BlockRange) -> str:
    """DSL spelling of a static block range: ``DS.select(1, 10)``."""
    text = f"{block_range.block.name}.select({block_range.start}, {block_range.end})"
    if block_range.reverse_order:
        text += ".reverse()"
    return text


class SumExpr(Expression):
    """Sum of all tag values in a block range."""

    def __init__(self, block_range: BlockRange) -> None:
        self.block_range = block_range

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        return sum(ctx.get_tag(tag.name, 0) for tag in self.block_range)

    def __repr__(self) -> str:
        return f"{_range_dsl(self.block_range)}.sum()"


#: Range aggregates other than sum, in pyrung spelling (CLICK: MAX, MIN, MDN, AVG).
RANGE_AGGREGATE_KINDS: tuple[str, ...] = ("max", "min", "median", "mean")


def aggregate_values(kind: str, values: list[Any] | tuple[Any, ...]) -> Numeric:
    """Compute one range aggregate over already-read member values.

    Shared by :class:`RangeAggregateExpr` and crossing forward evaluation so
    every consumer computes the same number.  The compiled backend emits the
    same operations inline (``_primitives.py``), in the same member order:

    - ``max`` / ``min``: Python ``max`` / ``min``.
    - ``mean``: left-to-right ``+`` then ``/ n`` (exact quotient; calc's
      destination conversion decides truncation).
    - ``median``: middle of the sorted values; for an even count, the mean of
      the two middle values (truncated toward zero when the range is
      integer, as on a CLICK).

    An empty range yields 0.
    """
    if not values:
        return 0
    if kind == "max":
        return max(values)
    if kind == "min":
        return min(values)
    if kind == "mean":
        total = values[0]
        for value in values[1:]:
            total = total + value
        return total / len(values)
    if kind == "median":
        ordered = sorted(values)
        mid = len(ordered) // 2
        if len(ordered) % 2:
            return ordered[mid]
        pair = ordered[mid - 1] + ordered[mid]
        if type(pair) is int:
            # CLICK: MDN of an integer range is an integer, the mean of the
            # middle two truncated toward zero.
            return pair // 2 if pair >= 0 else -(-pair // 2)
        return pair / 2
    raise ValueError(f"Unknown range aggregate kind: {kind!r}")


class RangeAggregateExpr(Expression):
    """``max`` / ``min`` / ``median`` / ``mean`` of the tag values in a block range.

    A sibling of :class:`SumExpr`, not a subclass: every ``isinstance(x, SumExpr)``
    arm computes or exports a sum, and must not see these.
    """

    def __init__(self, kind: str, block_range: BlockRange) -> None:
        if kind not in RANGE_AGGREGATE_KINDS:
            raise ValueError(
                f"Unknown range aggregate kind {kind!r}; expected one of "
                f"{', '.join(RANGE_AGGREGATE_KINDS)}"
            )
        self.kind = kind
        self.block_range = block_range

    def evaluate(self, ctx: ScanContext | ConditionView) -> Numeric:
        values = [ctx.get_tag(tag.name, tag.default) for tag in self.block_range]
        return aggregate_values(self.kind, values)

    def __repr__(self) -> str:
        return f"{_range_dsl(self.block_range)}.{self.kind}()"


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
    # Range aggregates — DSL method form, e.g. DS.select(1, 5).sum()
    if isinstance(expr, SumExpr):
        return f"{_range_dsl(expr.block_range)}.sum()"
    if isinstance(expr, RangeAggregateExpr):
        return f"{_range_dsl(expr.block_range)}.{expr.kind}()"
    if isinstance(expr, RandIntExpr):
        return f"randint({format_expr(expr.lo)}, {format_expr(expr.hi)})"
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
