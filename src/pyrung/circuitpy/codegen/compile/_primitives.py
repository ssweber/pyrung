"""Automatically generated module split."""

from __future__ import annotations

import math
import re
from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _DINT_MIN,
    _FAULT_ADDRESS_ERROR_TAG,
    _FAULT_OUT_OF_RANGE_TAG,
    _INT_MAX,
    _INT_MIN,
    _TYPE_DEFAULTS,
)
from pyrung.circuitpy.codegen._util import (
    _bool_literal,
    _indent_body,
)
from pyrung.circuitpy.codegen.context import (
    CodegenContext,
)
from pyrung.core.expression import (
    ABS_FLOORS,
    ABS_OF_TYPE,
    DOMAIN_LIMITED_MATH_FUNCS,
    DOUBLE_EXACT_LIMIT,
    BinaryExpr,
    Expression,
    LiteralExpr,
    MathFuncExpr,
    RandIntExpr,
    RangeAggregateExpr,
    ShiftFuncExpr,
    SumExpr,
    TagExpr,
    UnaryExpr,
    may_be_float,
)
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.time_mode import TimeUnit


def _snapshot_tag_symbol(
    tag: Tag | ImmediateRef,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    if isinstance(tag, ImmediateRef):
        tag = tag.tag

    entry_symbol = ctx.fault_entry_symbols.get(tag.name)
    if entry_symbol is not None:
        ctx.fault_entry_reads.add(tag.name)
        return entry_symbol

    block_info = ctx.tag_block_addresses.get(tag.name)
    if block_info is not None and block_snapshots is not None:
        block_id, addr = block_info
        block_symbol = block_snapshots.get(block_id)
        if block_symbol is not None:
            binding = ctx.block_bindings.get(block_id)
            if binding is None:
                raise RuntimeError(f"Missing block binding for condition tag {tag.name!r}")
            return f"{block_symbol}[{ctx.block_index(block_id, addr)}]"

    if scalar_snapshots is not None:
        snapshot_symbol = scalar_snapshots.get(tag.name)
        if snapshot_symbol is not None:
            return snapshot_symbol

    if ctx.blockless and (block_info is not None or tag.name in ctx.dict_backed_tag_names):
        return f"tags.get({tag.name!r}, {tag.default!r})"

    return ctx.symbol_for_tag(tag)


def _block_name_expr(
    block_id: int,
    index_expr: str,
    ctx: CodegenContext,
) -> str:
    return f"{ctx.block_name_tuple_symbol(block_id)}[{index_expr}]"


def _range_item_read_expr(
    range_value: BlockRange | IndirectBlockRange,
    symbol: str,
    key_expr: str,
    ctx: CodegenContext,
) -> str:
    if isinstance(range_value, BlockRange):
        layout = ctx.static_range_layout(range_value)
        if not layout.uses_backing_storage:
            return f"{symbol}[{key_expr}]"
    if not ctx.blockless:
        return f"{symbol}[{key_expr}]"
    binding = ctx.block_bindings[id(range_value.block)]
    default = _TYPE_DEFAULTS[binding.tag_type]
    return f"{symbol}.get({key_expr}, {default!r})"


def _range_item_write_expr(symbol: str, key_expr: str, value_expr: str) -> str:
    return f"{symbol}[{key_expr}] = {value_expr}"


def _compile_expression_impl(
    expr: Expression,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
    strict: bool = False,
) -> str:
    """Return a Python expression string with explicit parentheses.

    *strict* is for calc, whose expression raises on a math error (calc stores
    0 and sets the flag).  Anywhere else an expression is permissive, as the
    core's: an operation that can raise goes through ``_permissive_op`` /
    ``_permissive_math``, which give 0 instead.
    """
    if isinstance(expr, TagExpr):
        return _snapshot_tag_symbol(
            expr.tag,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(expr, LiteralExpr):
        return repr(expr.value)

    if isinstance(expr, BinaryExpr):
        left = _compile_expression_impl(
            expr.left,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        right = _compile_expression_impl(
            expr.right,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        if not strict and (
            _binary_may_raise(expr) or _may_be_huge(expr.left) or _may_be_huge(expr.right)
        ):
            return _permissive_op(expr.symbol, left, right, ctx)
        if expr.symbol in _BITWISE_SYMBOLS:
            left_int = _bit_int_expr(expr.left, left, ctx)
            right_int = _bit_int_expr(expr.right, right, ctx)
            if expr.symbol in ("<<", ">>"):
                right_int = _shift_count_expr(expr.right, right_int, ctx)
            return _calc_step(expr, f"({left_int} {expr.symbol} {right_int})", ctx, strict)
        if expr.symbol == "**" and (
            _may_be_float(expr.left)
            or _may_be_float(expr.right)
            or not _positive_int_literal(expr.right)
        ):
            # _pow gives +/-Inf for a float overflow, as the core's _op_pow,
            # raises _MathDomainError for a complex result and _QuietMathError
            # for a zero base to a power of 0 or below; an integer power is
            # exact and never overflows.
            ctx.mark_helper("_math_func")
            ctx.mark_helper("_QuietMathError")
            ctx.mark_helper("_pow")
            return _calc_step(expr, f"_pow({left}, {right})", ctx, strict)
        if expr.symbol == "%" and not (_non_negative(expr.left) and _positive_literal(expr.right)):
            # MOD truncates (the dividend's sign) and a MOD by 0 is a
            # _QuietMathError, as the core's _op_mod; Python's % floors.
            ctx.mark_helper("_QuietMathError")
            ctx.mark_helper("_mod")
            return f"_mod({left}, {right})"
        return _calc_step(expr, f"({left} {expr.symbol} {right})", ctx, strict)

    if isinstance(expr, UnaryExpr):
        inner = _compile_expression_impl(
            expr.operand,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        if expr.symbol == "abs":
            floor = _abs_floor(expr)
            if floor is not None:
                # abs() of an Int or Dint tag in a calc (calc_form): the bottom of
                # the range raises OverflowError, 0 with SC43.
                ctx.mark_helper("_abs_int")
                return f"_abs_int({inner}, {floor})"
            return _calc_step(expr, f"abs({inner})", ctx, strict)
        if expr.symbol == "~":
            if not strict and _may_be_float(expr.operand):
                return _permissive_op("~", inner, "0", ctx)
            return _calc_step(expr, f"(~{_bit_int_expr(expr.operand, inner, ctx)})", ctx, strict)
        return _calc_step(expr, f"({expr.symbol}({inner}))", ctx, strict)

    if isinstance(expr, MathFuncExpr):
        if expr.name not in _ALLOWED_MATH_FUNCS:
            raise TypeError(f"Unsupported expression type: {type(expr).__name__}")
        # _math_func passes a non-finite operand through and raises
        # _MathDomainError for a domain error, as MathFuncExpr.evaluate does;
        # outside calc _permissive_math gives 0 for it instead, and for an
        # integer too large for a float (OverflowError).
        ctx.mark_helper("_math_func")
        helper = "_math_func"
        if not strict and (expr.name in DOMAIN_LIMITED_MATH_FUNCS or _may_be_huge(expr.operand)):
            ctx.mark_helper("_permissive_math")
            helper = "_permissive_math"
        return (
            f"{helper}(math.{expr.name}, "
            f"{_compile_expression_impl(expr.operand, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots, strict=strict)}"
            ")"
        )

    if isinstance(expr, ShiftFuncExpr):
        value = _compile_expression_impl(
            expr.value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        count = _compile_expression_impl(
            expr.count,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        if not strict and (
            _may_be_float(expr.value)
            or _may_be_float(expr.count)
            or (expr.name in ("lsh", "rsh") and not _non_negative_literal(expr.count))
        ):
            return _permissive_op(expr.name, value, count, ctx)
        value_int = _bit_int_expr(expr.value, value, ctx)
        count_int = _bit_int_expr(expr.count, count, ctx)
        if expr.name in ("lsh", "rsh"):
            count_int = _shift_count_expr(expr.count, count_int, ctx)
        if expr.name == "lsh":
            return _calc_step(expr, f"({value_int} << {count_int})", ctx, strict)
        if expr.name == "rsh":
            return _calc_step(expr, f"({value_int} >> {count_int})", ctx, strict)
        if expr.name == "lro":
            return (
                f"(((({value_int} & 0xFFFF) << ({count_int} % 16)) | "
                f"(({value_int} & 0xFFFF) >> (16 - ({count_int} % 16)))) & 0xFFFF)"
            )
        if expr.name == "rro":
            return (
                f"(((({value_int} & 0xFFFF) >> ({count_int} % 16)) | "
                f"(({value_int} & 0xFFFF) << (16 - ({count_int} % 16)))) & 0xFFFF)"
            )
        raise TypeError(f"Unsupported expression type: {type(expr).__name__}")

    if isinstance(expr, SumExpr):
        terms = [
            _snapshot_tag_symbol(
                tag,
                ctx,
                scalar_snapshots=scalar_snapshots,
                block_snapshots=block_snapshots,
            )
            for tag in expr.block_range
        ]
        return f"({' + '.join(terms)})"

    if isinstance(expr, RandIntExpr):
        # As RandIntExpr.evaluate: a bare Real tag as a bound reads 0, and equal
        # bounds, or one holding Inf or NaN, make the site 0.  In a calc the site
        # appends its flag to the calc's _calc_flags (CALC_FLAGS), which the calc
        # sets after the formula: only the site is zeroed.
        if expr.real_bounds:
            return "0"
        flags = CALC_FLAGS if strict else "None"
        if expr.equal_bounds:
            if not strict:
                return "0"
            ctx.mark_helper("_rand_fail")
            return f"_rand_fail({flags}, 46)"
        draw = ""
        if ctx.kernel_runtime:
            # The runner's input drain wrote this scan's draw into the site tag.
            draw = _snapshot_tag_symbol(
                expr.tag,
                ctx,
                scalar_snapshots=scalar_snapshots,
                block_snapshots=block_snapshots,
            )
            if not expr.reads_bounds:
                return draw
        lo = _compile_expression_impl(
            expr.lo,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        hi = _compile_expression_impl(
            expr.hi,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        if expr.reads_bounds:
            ctx.mark_helper("_bit_int")
            ctx.mark_helper("_rand_fail")
            if ctx.kernel_runtime:
                ctx.mark_helper("_rand_term")
                return f"_rand_term({flags}, {draw}, {lo}, {hi})"
            ctx.uses_random = True
            ctx.mark_helper("_rand_draw")
            return f"_rand_draw({flags}, {lo}, {hi})"
        ctx.uses_random = True
        lo, hi = _bit_int_expr(expr.lo, lo, ctx), _bit_int_expr(expr.hi, hi, ctx)
        return f"random.randint(min({lo}, {hi}), max({lo}, {hi}))"

    if isinstance(expr, RangeAggregateExpr):
        return _compile_range_aggregate(
            expr,
            [
                _snapshot_tag_symbol(
                    tag,
                    ctx,
                    scalar_snapshots=scalar_snapshots,
                    block_snapshots=block_snapshots,
                )
                for tag in expr.block_range
            ],
        )

    raise TypeError(f"Unsupported expression type: {type(expr).__name__}")


def _compile_range_aggregate(expr: RangeAggregateExpr, terms: list[str]) -> str:
    """Inline ``max``/``min``/``median``/``mean`` over a static range.

    Mirrors :func:`pyrung.core.expression.aggregate_values` operation for
    operation (same member order, same left-to-right sum), so the interpreted
    and compiled backends store identical values. CircuitPython has no
    ``statistics`` module, so median and mean are spelled out.
    """
    if not terms:
        return "0"
    count = len(terms)
    members = f"({', '.join(terms)},)"
    if expr.kind == "max":
        return f"max({members})"
    if expr.kind == "min":
        return f"min({members})"
    if expr.kind == "mean":
        return f"(({' + '.join(terms)}) / {count})"
    if expr.kind == "median":
        mid = count // 2
        if count % 2:
            return f"sorted({members})[{mid}]"
        return (
            f"((lambda _s: (lambda _t: (_t // 2 if _t >= 0 else -(-_t // 2)) "
            f"if type(_t) is int else _t / 2)(_s[{mid - 1}] + _s[{mid}]))(sorted({members})))"
        )
    raise TypeError(f"Unsupported range aggregate kind: {expr.kind!r}")


_BITWISE_SYMBOLS = frozenset({"&", "|", "^", "<<", ">>"})


def _non_zero_literal(expr: Any) -> bool:
    return isinstance(expr, LiteralExpr) and expr.value != 0


def _positive_literal(expr: Any) -> bool:
    return isinstance(expr, LiteralExpr) and expr.value > 0


def _non_negative_literal(expr: Any) -> bool:
    return isinstance(expr, LiteralExpr) and expr.value >= 0


def _non_negative_int_literal(expr: Any) -> bool:
    return isinstance(expr, LiteralExpr) and isinstance(expr.value, int) and expr.value >= 0


def _positive_int_literal(expr: Any) -> bool:
    return isinstance(expr, LiteralExpr) and isinstance(expr.value, int) and expr.value > 0


#: The list a calc's randint() sites append their flags to (``_rand_fail``).
CALC_FLAGS = "_calc_flags"

#: Tag types whose value is never negative.
_UNSIGNED_TYPES = frozenset({TagType.BOOL, TagType.WORD})


def _non_negative(expr: Any) -> bool:
    """Whether *expr* is a non-negative constant or a Bool or Word tag."""
    if isinstance(expr, LiteralExpr):
        return expr.value >= 0
    return isinstance(expr, TagExpr) and expr.tag.type in _UNSIGNED_TYPES


def _abs_floor(expr: UnaryExpr) -> int | None:
    """The range bottom an ``abs()`` that calc_form swapped (ABS_OF_TYPE) refuses."""
    for type_name, op in ABS_OF_TYPE.items():
        if expr.op is op:
            return ABS_FLOORS[type_name]
    return None


def calc_site_flags(expr: Any) -> bool:
    """Whether a calc of *expr* has a randint() site that can append a flag."""
    if isinstance(expr, RandIntExpr):
        return not expr.real_bounds and (expr.equal_bounds or expr.reads_bounds)
    if isinstance(expr, Expression):
        return any(calc_site_flags(child) for child in vars(expr).values())
    return False


#: The largest magnitude of each integer tag type.
_TYPE_BOUNDS = {
    TagType.BOOL: 1,
    TagType.INT: 1 << 15,
    TagType.DINT: 1 << 31,
    TagType.WORD: 0xFFFF,
    TagType.REAL: 0,  # a Real holds a float: an integer bound does not apply
}


def _int_bound(expr: Any) -> float:
    """A bound on the magnitude of *expr* when it is an integer (inf if unknown).

    A step whose bound is past ``DOUBLE_EXACT_LIMIT`` goes through ``_dbl`` in a
    calc, as the core's ``_calc_step``; a float result needs no bound (0).
    """
    if isinstance(expr, bool):
        return 1
    if isinstance(expr, int):
        return abs(expr)
    if isinstance(expr, float):
        return 0
    if isinstance(expr, LiteralExpr):
        return _int_bound(expr.value)
    if isinstance(expr, TagExpr):
        return _TYPE_BOUNDS.get(expr.tag.type, math.inf)
    if isinstance(expr, BinaryExpr):
        left, right = _int_bound(expr.left), _int_bound(expr.right)
        symbol = expr.symbol
        if symbol in ("+", "-"):
            return left + right
        if symbol == "*":
            return left * right
        if symbol == "/":
            return 0
        if symbol in ("//", "%", ">>"):
            return left
        if symbol in ("&", "|", "^"):
            return 2 * max(left, right) + 1
        if symbol == "<<" and _small_count_literal(expr.right):
            return left * (1 << expr.right.value)
        if symbol == "**" and _small_count_literal(expr.right):
            return left**expr.right.value
        return math.inf
    if isinstance(expr, UnaryExpr):
        return _int_bound(expr.operand) + (1 if expr.symbol == "~" else 0)
    if isinstance(expr, MathFuncExpr):
        return 0
    if isinstance(expr, ShiftFuncExpr):
        if expr.name in ("lro", "rro"):
            return 0xFFFF
        if expr.name == "rsh":
            return _int_bound(expr.value)
        if _small_count_literal(expr.count):
            return _int_bound(expr.value) * (1 << expr.count.value)
        return math.inf
    if isinstance(expr, RandIntExpr):
        return 1 << 31
    if isinstance(expr, SumExpr):
        return sum(_TYPE_BOUNDS.get(tag.type, math.inf) for tag in expr.block_range)
    if isinstance(expr, RangeAggregateExpr):
        return max((_TYPE_BOUNDS.get(tag.type, math.inf) for tag in expr.block_range), default=0)
    return math.inf


def _small_count_literal(expr: Any) -> bool:
    return _non_negative_int_literal(expr) and expr.value <= 64


def _calc_step(expr: Any, compiled: str, ctx: CodegenContext, strict: bool) -> str:
    """*compiled*, a step of a calc, as a double past 2 ** 53 (core ``_calc_step``)."""
    if not strict or _int_bound(expr) <= DOUBLE_EXACT_LIMIT:
        return compiled
    ctx.mark_helper("_dbl")
    return f"_dbl({compiled})"


def _binary_may_raise(expr: BinaryExpr) -> bool:
    """Whether *expr* can raise on a math error (and so, outside calc, give 0).

    A division by a non-zero constant, a power with a positive integer
    constant exponent, a bitwise operation on integers and a shift by a
    non-negative constant cannot; ``+``, ``-`` and ``*`` never do (a float
    overflow is Inf).  Each of them can raise when an operand is an integer too
    large for a float (:func:`_may_be_huge`), which the caller checks.
    """
    symbol = expr.symbol
    if symbol in ("/", "//", "%"):
        return not _non_zero_literal(expr.right)
    if symbol == "**":
        # A zero base to a power of 0 or below is a math error, and a
        # fractional power of a negative base is complex (one too).
        return not _positive_int_literal(expr.right)
    if symbol in ("&", "|", "^"):
        return _may_be_float(expr.left) or _may_be_float(expr.right)
    if symbol in ("<<", ">>"):
        return (
            _may_be_float(expr.left)
            or _may_be_float(expr.right)
            or not _non_negative_literal(expr.right)
        )
    return False


def _may_be_huge(expr: Any) -> bool:
    """Whether *expr* may be an integer too large for a float (past 2 ** 1024).

    A ``**`` makes one from tags (``2 ** E``, ``BD ** 40``), and so can a left
    shift by more than a literal 64.  Such an integer is exact, but an operation
    that turns it into a float (``* 0.5``, ``/``, ``sin``) raises
    ``OverflowError``.  ``+``, ``-``, ``*`` of tags alone stay far below it.
    """
    if isinstance(expr, BinaryExpr):
        if expr.symbol == "**":
            return True
        if expr.symbol == "<<" and not _small_shift_literal(expr.right):
            return True
        return _may_be_huge(expr.left) or _may_be_huge(expr.right)
    if isinstance(expr, ShiftFuncExpr):
        if expr.name == "lsh" and not _small_shift_literal(expr.count):
            return True
        return _may_be_huge(expr.value) or _may_be_huge(expr.count)
    if isinstance(expr, UnaryExpr):
        return _may_be_huge(expr.operand)
    return False


def _small_shift_literal(expr: Any) -> bool:
    return isinstance(expr, LiteralExpr) and isinstance(expr.value, int) and expr.value <= 64


def _shift_count_expr(expr: Any, compiled: str, ctx: CodegenContext) -> str:
    """A shift count; a negative one raises OverflowError (core ``shift_count``)."""
    if _non_negative_literal(expr):
        return compiled
    ctx.mark_helper("_bit_int")
    ctx.mark_helper("_shift_count")
    return f"_shift_count({compiled})"


def _permissive_op(op: str, left: str, right: str, ctx: CodegenContext) -> str:
    """``_permissive_op(op, left, right)``: the operation, or 0 for a math error.

    The core's expressions outside calc give 0 where an operation raises
    (``_permissive`` in pyrung.core.expression).
    """
    ctx.mark_helper("_bit_int")
    ctx.mark_helper("_math_func")
    ctx.mark_helper("_QuietMathError")
    ctx.mark_helper("_mod")
    ctx.mark_helper("_pow")
    ctx.mark_helper("_permissive_op")
    return f"_permissive_op({op!r}, {left}, {right})"


_may_be_float = may_be_float


def _bit_int_expr(expr: Any, compiled: str, ctx: CodegenContext) -> str:
    """``int(compiled)`` for a bitwise or shift operand.

    An operand that may be NaN goes through ``_bit_int``, which raises
    ``OverflowError`` for NaN as ``int()`` does for Inf, so the calc stores 0
    with the out-of-range flag (as the core's ``bit_operand``), instead of
    the ``ValueError`` of ``int(nan)`` stopping the scan.
    """
    if _may_be_float(expr):
        ctx.mark_helper("_bit_int")
        return f"_bit_int({compiled})"
    return f"int({compiled})"


_ALLOWED_MATH_FUNCS = frozenset(
    {
        "sqrt",
        "sin",
        "cos",
        "tan",
        "asin",
        "acos",
        "atan",
        "radians",
        "degrees",
        "log10",
        "log",
    }
)


def _copy_converter_target_info(
    target: Tag | IndirectRef | IndirectExprRef,
    ctx: CodegenContext,
    stem: str,
) -> tuple[list[str], str, str, str | None, str]:
    if isinstance(target, Tag):
        block_info = ctx.tag_block_addresses.get(target.name)
        if block_info is None or ctx.blockless:
            return [], "scalar", _compile_lvalue(target, ctx), None, target.type.name
        block_id, addr = block_info
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for tag-backed target {target.name!r}")
        symbol = ctx.symbol_for_block(binding.block)
        start_var = f"_{stem}_start_idx"
        return (
            [f"{start_var} = {ctx.block_index(block_id, addr)}"],
            "block",
            symbol,
            start_var,
            target.type.name,
        )

    if isinstance(target, IndirectRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        ptr = _compile_value(target.pointer, ctx)
        if ctx.blockless:
            return (
                [f"{start_var} = {helper}(int({ptr}))"],
                "blockless_dynamic",
                ctx.block_name_tuple_symbol(binding.block_id),
                start_var,
                binding.tag_type.name,
            )
        return (
            [f"{start_var} = {helper}(int({ptr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    if isinstance(target, IndirectExprRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        expr = _compile_expression_impl(target.expr, ctx)
        if ctx.blockless:
            return (
                [f"{start_var} = {helper}(int({expr}))"],
                "blockless_dynamic",
                ctx.block_name_tuple_symbol(binding.block_id),
                start_var,
                binding.tag_type.name,
            )
        return (
            [f"{start_var} = {helper}(int({expr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    raise TypeError(f"Unsupported copy modifier target type: {type(target).__name__}")


_SEQUENTIAL_TAG_RE = re.compile(r"^(.*?)(\d+)$")


def _parse_sequential_tag_name(name: str) -> tuple[str, int, int] | None:
    m = _SEQUENTIAL_TAG_RE.match(name)
    if m is None:
        return None
    prefix, suffix = m.groups()
    return prefix, int(suffix), len(suffix)


def _compute_sequential_reloads(
    target_name: str,
    ctx: CodegenContext,
) -> list[tuple[int, str]] | None:
    seq = _parse_sequential_tag_name(target_name)
    if seq is None:
        return None
    prefix, base, width = seq
    reloads: list[tuple[int, str]] = []
    for tag_name in list(ctx.referenced_tags):
        if (
            tag_name == target_name
            or tag_name in ctx.tag_block_addresses
            or tag_name in ctx.dict_backed_tag_names
        ):
            continue
        m = _SEQUENTIAL_TAG_RE.match(tag_name)
        if m is None:
            continue
        t_prefix = m.group(1)
        if t_prefix != prefix:
            continue
        t_idx = int(m.group(2))
        offset = t_idx - base
        if offset <= 0:
            continue
        expected_name = f"{prefix}{t_idx:0{width}d}"
        if expected_name != tag_name:
            continue
        tag = ctx.referenced_tags[tag_name]
        symbol = ctx.symbol_for_tag(tag)
        reloads.append((offset, symbol))
    return sorted(reloads) if reloads else None


def _copy_converter_write_lines(
    *,
    values_var: str,
    target_kind: str,
    target_symbol: str,
    target_start_var: str | None,
    fault_body: list[str],
    target_name: str | None = None,
    sequential_reloads: list[tuple[int, str]] | None = None,
    kernel: bool = False,
) -> list[str]:
    if target_kind == "scalar":
        seq = _parse_sequential_tag_name(target_name) if target_name else None
        if seq is not None:
            prefix, base, width = seq
            lines = [
                f"if len({values_var}) >= 1:",
                f"    {target_symbol} = {values_var}[0]",
            ]
            if kernel:
                # The kernel keeps every tag in ``tags``; generated board code has
                # no such dict and stores only the registers it references (below).
                lines += [
                    f"    for _seq_i in range(1, len({values_var})):",
                    f'        tags[f"{prefix}{{{base} + _seq_i:0{width}d}}"] = {values_var}[_seq_i]',
                ]
            if sequential_reloads:
                for offset, symbol in sequential_reloads:
                    lines.append(f"    if len({values_var}) > {offset}:")
                    lines.append(f"        {symbol} = {values_var}[{offset}]")
            return lines
        return [
            f"if len({values_var}) > 1:",
            *_indent_body(fault_body, 4),
            f"elif len({values_var}) == 1:",
            f"    {target_symbol} = {values_var}[0]",
        ]

    if target_kind == "blockless_dynamic":
        if target_start_var is None:
            raise RuntimeError("blockless dynamic target is missing start index")
        return [
            f"_copy_count = len({values_var})",
            "if _copy_count == 0:",
            "    pass",
            f"elif ({target_start_var} < 0) or (({target_start_var} + _copy_count) > len({target_symbol})):",
            *_indent_body(fault_body, 4),
            "else:",
            f"    for _copy_offset, _copy_value in enumerate({values_var}):",
            f"        tags[{target_symbol}[{target_start_var} + _copy_offset]] = _copy_value",
        ]

    if target_start_var is None:
        raise RuntimeError("copy modifier block target is missing start index")
    return [
        f"_copy_count = len({values_var})",
        "if _copy_count == 0:",
        "    pass",
        f"elif ({target_start_var} < 0) or (({target_start_var} + _copy_count) > len({target_symbol})):",
        *_indent_body(fault_body, 4),
        "else:",
        f"    for _copy_offset, _copy_value in enumerate({values_var}):",
        f"        {target_symbol}[{target_start_var} + _copy_offset] = _copy_value",
    ]


def fault_follow_scope(instr: Any, ctx: CodegenContext) -> None:
    """Open the scope of *instr*'s followed flags, before its body compiles."""
    ctx.fault_entry_reads = set()
    ctx.fault_entry_symbols = {
        name: f"_fault_in_{name.rpartition('.')[2]}" for name in instr._fault_follows
    }


def _fault_follow_lines(instr: Any, ctx: CodegenContext) -> list[str]:
    """Clear *instr*'s followed flags as it starts: a raise in its body sets them.

    The flag's entry value goes in a local first when the body's source reads it.
    """
    entry_symbols = ctx.fault_entry_symbols
    reads = ctx.fault_entry_reads
    ctx.fault_entry_symbols = {}
    ctx.fault_entry_reads = set()
    lines: list[str] = []
    for name in instr._fault_follows:
        if name in reads:
            lines.append(f"{entry_symbols[name]} = {ctx.symbol_if_referenced(name)}")
    lines.extend(f"{ctx.symbol_if_referenced(name)} = False" for name in instr._fault_follows)
    return lines


def _compile_guarded_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    enabled_body: list[str],
    *,
    disabled_body: list[str] | None = None,
) -> list[str]:
    sp = " " * indent
    lines: list[str] = []
    enabled_body = [*_fault_follow_lines(instr, ctx), *enabled_body]
    if ctx.used_indirect_blocks:
        enabled_body = [
            "try:",
            *[f"    {line}" for line in enabled_body],
            "except IndexError:",
            "    pass",
        ]
    enabled_literal = _bool_literal(enabled_expr)
    if getattr(instr, "oneshot", False):
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        if enabled_literal is False:
            lines.append(f"{sp}_mem[{key!r}] = False")
            if disabled_body:
                lines.extend(f"{sp}{line}" for line in disabled_body)
            return lines
        if enabled_literal is True:
            lines.append(f"{sp}if not bool(_mem.get({key!r}, False)):")
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
            lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
            return lines
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = False")
        if disabled_body:
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
        return lines

    if enabled_literal is True:
        lines.extend(f"{sp}{line}" for line in enabled_body)
        return lines
    if enabled_literal is False:
        if disabled_body is not None:
            lines.extend(f"{sp}{line}" for line in disabled_body)
        return lines

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
    if disabled_body:
        lines.append(f"{sp}else:")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
    return lines


def _compile_assignment_lines(
    target: Tag | IndirectRef | IndirectExprRef,
    value_expr: str,
    ctx: CodegenContext,
    *,
    indent: int,
) -> list[str]:
    lvalue = _compile_lvalue(target, ctx)
    return [f"{' ' * indent}{lvalue} = {value_expr}"]


def _compile_lvalue(target: Tag | IndirectRef | IndirectExprRef, ctx: CodegenContext) -> str:
    if isinstance(target, Tag):
        if ctx.blockless and (
            target.name in ctx.tag_block_addresses or target.name in ctx.dict_backed_tag_names
        ):
            return f"tags[{target.name!r}]"
        return ctx.symbol_for_tag(target)
    if isinstance(target, IndirectRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        helper = ctx.use_indirect_block(binding.block_id)
        ptr = _compile_value(target.pointer, ctx)
        index_expr = f"{helper}(int({ptr}))"
        if ctx.blockless:
            return f"tags[{_block_name_expr(binding.block_id, index_expr, ctx)}]"
        block_symbol = ctx.symbol_for_block(target.block)
        return f"{block_symbol}[{index_expr}]"
    if isinstance(target, IndirectExprRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        helper = ctx.use_indirect_block(binding.block_id)
        expr = _compile_expression_impl(target.expr, ctx)
        index_expr = f"{helper}(int({expr}))"
        if ctx.blockless:
            return f"tags[{_block_name_expr(binding.block_id, index_expr, ctx)}]"
        block_symbol = ctx.symbol_for_block(target.block)
        return f"{block_symbol}[{index_expr}]"
    raise TypeError(f"Unsupported assignment target: {type(target).__name__}")


def _compile_range_setup(
    range_value: Any,
    ctx: CodegenContext,
    *,
    stem: str,
    include_addresses: bool,
) -> tuple[list[str], str, str, str]:
    if not isinstance(range_value, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}"
        )
    if isinstance(range_value, BlockRange):
        name = ctx.next_name(stem)
        indices_var = f"_{name}_{'names' if ctx.blockless else 'indices'}"
        addrs_var = f"_{name}_addrs"
        layout = ctx.static_range_layout(range_value)
        addresses = list(layout.addresses)
        if not layout.uses_backing_storage:
            symbol = f"_{name}_values"
            value_exprs = [_compile_value(tag, ctx) for tag in layout.tags]
            lines = [f"{symbol} = [{', '.join(value_exprs)}]"]
            indices_expr = _sequence_expr(list(range(len(layout.tags))))
            lines.append(f"{indices_var} = {indices_expr}")
            if include_addresses:
                lines.append(f"{addrs_var} = {_sequence_expr(addresses)}")
            else:
                addrs_var = "[]"
            return lines, symbol, indices_var, addrs_var
        binding = ctx.block_bindings[id(range_value.block)]
        symbol = "tags" if ctx.blockless else ctx.symbol_for_block(range_value.block)
        if ctx.blockless:
            indices_expr = repr(tuple(binding.block._get_tag(addr).name for addr in addresses))
        else:
            indices = [ctx.block_index(binding.block_id, addr) for addr in addresses]
            indices_expr = _sequence_expr(indices)
        lines = [f"{indices_var} = {indices_expr}"]
        if include_addresses:
            lines.append(f"{addrs_var} = {_sequence_expr(addresses)}")
        else:
            addrs_var = "[]"
        return lines, symbol, indices_var, addrs_var

    binding = ctx.block_bindings[id(range_value.block)]
    symbol = "tags" if ctx.blockless else ctx.symbol_for_block(range_value.block)
    helper = ctx.use_window_addrs_block(binding.block_id)
    name = ctx.next_name(stem)
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_{'name' if ctx.blockless else 'idx'}"
    indices_var = f"_{name}_{'names' if ctx.blockless else 'indices'}"
    addrs_var = f"_{name}_addrs"
    start_expr = _compile_address_expr(range_value.start_expr, ctx)
    end_expr = _compile_address_expr(range_value.end_expr, ctx)
    lines = [
        f"{start_var} = int({start_expr})",
        f"{end_var} = int({end_expr})",
        f"if {start_var} > {end_var}:",
        '    raise ValueError("Indirect range start must be <= end")',
        f"{indices_var} = []",
    ]
    if include_addresses:
        lines.append(f"{addrs_var} = []")
    else:
        addrs_var = "[]"
    lines.append(f"for {addr_var} in {helper}({start_var}, {end_var}):")
    resolved_expr = f"{addr_var} - {binding.start}"
    if ctx.blockless:
        name_expr = _block_name_expr(binding.block_id, resolved_expr, ctx)
        lines.append(f"    {idx_var} = {name_expr}")
    else:
        lines.append(f"    {idx_var} = {resolved_expr}")
    lines.append(f"    {indices_var}.append({idx_var})")
    if include_addresses:
        lines.append(f"    {addrs_var}.append(int({addr_var}))")
    if range_value.reverse_order:
        lines.append(f"{indices_var}.reverse()")
        if include_addresses:
            lines.append(f"{addrs_var}.reverse()")
    return lines, symbol, indices_var, addrs_var


def _compile_range_flush(
    range_value: BlockRange | IndirectBlockRange,
    symbol: str,
    ctx: CodegenContext,
) -> list[str]:
    """Flush a specialized static range's value list to its semantic tags."""

    if not isinstance(range_value, BlockRange):
        return []
    layout = ctx.static_range_layout(range_value)
    if layout.uses_backing_storage:
        return []
    return [
        f"{_compile_lvalue(tag, ctx)} = {symbol}[{index}]" for index, tag in enumerate(layout.tags)
    ]


def _sequence_expr(values: list[int]) -> str:
    if not values:
        return "[]"
    if len(values) == 1:
        return repr(range(values[0], values[0] + 1))

    step = values[1] - values[0]
    if step != 0 and all(values[i + 1] - values[i] == step for i in range(len(values) - 1)):
        return repr(range(values[0], values[-1] + step, step))
    return repr(values)


def _search_compare_expr(condition: str, left_expr: str, right_expr: str) -> str:
    if condition == "==":
        return f"({left_expr} == {right_expr})"
    if condition == "!=":
        return f"({left_expr} != {right_expr})"
    if condition == "<":
        return f"({left_expr} < {right_expr})"
    if condition == "<=":
        return f"({left_expr} <= {right_expr})"
    if condition == ">":
        return f"({left_expr} > {right_expr})"
    if condition == ">=":
        return f"({left_expr} >= {right_expr})"
    raise ValueError(f"Unsupported search comparison: {condition!r}")


def _pack_store_expr(value_expr: str, dest_type: str, ctx: CodegenContext) -> str:
    if dest_type == "REAL":
        ctx.mark_helper("_int_to_float_bits")
        return f"_int_to_float_bits({value_expr})"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    raise TypeError(f"Unsupported pack destination type: {dest_type}")


def _calc_store_expr(value_expr: str, dest_type: str, mode: str, ctx: CodegenContext) -> str:
    if mode == "hex":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "BOOL":
        return f"bool({value_expr})"
    if dest_type == "REAL":
        return f"float({value_expr})"
    if dest_type == "CHAR":
        return value_expr
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    return value_expr


def _timer_carry_lines(acc: Tag, unit: TimeUnit, ctx: CodegenContext, sp: str) -> list[str]:
    """This scan's time into ``_int_units`` whole units and a ``_new_frac`` remainder.

    Integer nanoseconds, as ``instruction.utils.elapsed_units``.  A board's clock reads them
    directly, and its floats are too short to hold a time.  The kernel has only ``dt``.
    """
    ctx.timer_unit_ns[acc.name] = unit.ns
    dt_ns = '_dt_to_ns(_mem.get("_dt", 0.0))' if ctx.kernel_runtime else '_mem.get("_dt_ns", 0)'
    return [
        f'{sp}_carry = _mem.get("_frac:{acc.name}", 0) + {dt_ns}',
        f"{sp}_int_units = _carry // {unit.ns}",
        f"{sp}_new_frac = _carry % {unit.ns}",
    ]


def _load_cast_expr(value_expr: str, tag_type: str) -> str:
    if tag_type == "BOOL":
        return f"bool({value_expr})"
    if tag_type == "INT":
        return f"max({_INT_MIN}, min({_INT_MAX}, int({value_expr})))"
    if tag_type == "DINT":
        return f"max({_DINT_MIN}, min({_DINT_MAX}, int({value_expr})))"
    if tag_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if tag_type == "REAL":
        return f"float({value_expr})"
    if tag_type == "CHAR":
        # A blank TXT is NUL; a saved "" (or a non-string) loads as one.
        return f"(({value_expr} or '\\x00') if isinstance({value_expr}, str) else '\\x00')"
    return value_expr


def _compile_set_out_of_range_fault_body(ctx: CodegenContext) -> list[str]:
    fault_symbol = ctx.symbol_if_referenced(_FAULT_OUT_OF_RANGE_TAG)
    if fault_symbol is None:
        return ["pass"]
    return [f"{fault_symbol} = True"]


def _copy_clamp_condition(value_var: str, dest_type: str, source_type: str | None) -> str | None:
    """The test that a Copy of *value_var* into *dest_type* stops at a limit, if it can.

    It holds what the store truncates: 32767.5 into an INT stores 32767 without
    clamping.  +/-Inf passes and NaN compares false, as a clamp pins Inf and
    stores NaN as 0.  Text converts in the store and never clamps here.
    """
    bounds = {"INT": (-32768, 32767), "DINT": (-2147483648, 2147483647)}.get(dest_type)
    if bounds is None or source_type == "CHAR":
        return None
    lower, upper = bounds
    return f"{value_var} >= {upper + 1} or {value_var} <= {lower - 1}"


def _compile_set_address_error_fault_body(ctx: CodegenContext) -> list[str]:
    fault_symbol = ctx.symbol_if_referenced(_FAULT_ADDRESS_ERROR_TAG)
    if fault_symbol is None:
        return ["pass"]
    return [f"{fault_symbol} = True"]


def _compile_target_write_lines(
    target: Tag | BlockRange | IndirectBlockRange | ImmediateRef,
    value_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    if isinstance(target, ImmediateRef):
        return _compile_target_write_lines(target.value, value_expr, ctx, indent)
    if isinstance(target, Tag):
        return [f"{sp}{_compile_lvalue(target, ctx)} = {value_expr}"]

    if isinstance(target, BlockRange):
        layout = ctx.static_range_layout(target)
        if not layout.uses_backing_storage:
            lines = [f"{sp}{_compile_lvalue(tag, ctx)} = {value_expr}" for tag in layout.tags]
            return lines if lines else [f"{sp}pass"]

    if ctx.blockless:
        setup, symbol, indices_var, _ = _compile_range_setup(
            target,
            ctx,
            stem="targetwrite",
            include_addresses=False,
        )
        lines = [f"{sp}{line}" for line in setup]
        lines.extend(
            [
                f"{sp}for _target_name in {indices_var}:",
                f"{sp}    {_range_item_write_expr(symbol, '_target_name', value_expr)}",
            ]
        )
        return lines

    if isinstance(target, BlockRange):
        binding = ctx.block_bindings[id(target.block)]
        symbol = ctx.symbol_for_block(target.block)
        lines: list[str] = []
        for addr in target.addresses:
            index = ctx.block_index(binding.block_id, addr)
            lines.append(f"{sp}{symbol}[{index}] = {value_expr}")
        return lines if lines else [f"{sp}pass"]

    binding = ctx.block_bindings[id(target.block)]
    symbol = ctx.symbol_for_block(target.block)
    helper = ctx.use_window_addrs_block(binding.block_id)
    name = ctx.next_name("irange")
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_list = f"_{name}_indices"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_idx"
    start_expr = _compile_address_expr(target.start_expr, ctx)
    end_expr = _compile_address_expr(target.end_expr, ctx)
    lines = [
        f"{sp}{start_var} = int({start_expr})",
        f"{sp}{end_var} = int({end_expr})",
        f"{sp}if {start_var} > {end_var}:",
        f'{sp}    raise ValueError("Indirect range start must be <= end")',
        f"{sp}{addr_list} = []",
        f"{sp}for {addr_var} in {helper}({start_var}, {end_var}):",
        f"{sp}    {addr_list}.append({addr_var} - {binding.start})",
    ]
    if target.reverse_order:
        lines.append(f"{sp}{addr_list}.reverse()")
    lines.extend(
        [
            f"{sp}for {idx_var} in {addr_list}:",
            f"{sp}    {symbol}[{idx_var}] = {value_expr}",
        ]
    )
    return lines


def _compile_address_expr(
    addr: int | Tag | Any,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
) -> str:
    if isinstance(addr, int):
        return repr(addr)
    if isinstance(addr, Tag):
        return _snapshot_tag_symbol(
            addr,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(addr, Expression):
        return _compile_expression_impl(
            addr,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    raise TypeError(f"Unsupported indirect address expression type: {type(addr).__name__}")


def _compile_condition_element_read(
    block: Any,
    binding: Any,
    addr_expr: str,
    ctx: CodegenContext,
    block_snapshots: dict[int, str] | None,
) -> str:
    """A rung condition's read of ``block[addr]``: outside the block it reads 0, no flag."""
    index_expr = f"{ctx.use_condition_index_block(binding.block_id)}(int({addr_expr}))"
    return _element_read_or_default(block, binding, index_expr, ctx, block_snapshots)


def _compile_source_element_read(
    block: Any,
    binding: Any,
    addr_expr: str,
    ctx: CodegenContext,
    block_snapshots: dict[int, str] | None,
) -> str:
    """An instruction's read of ``block[addr]``: past the block 0, no flag; below it
    the address-error flag and an IndexError (``_read_index_*``)."""
    index_expr = f"{ctx.use_read_index_block(binding.block_id)}(int({addr_expr}))"
    return _element_read_or_default(block, binding, index_expr, ctx, block_snapshots)


def _element_read_or_default(
    block: Any,
    binding: Any,
    index_expr: str,
    ctx: CodegenContext,
    block_snapshots: dict[int, str] | None,
) -> str:
    """Read the element at *index_expr*, or the type's zero if it is -1."""
    block_id = id(block)
    default = _TYPE_DEFAULTS[binding.tag_type]
    if block_snapshots is not None and block_id in block_snapshots:
        ctx.mark_helper("_cond_elem")
        return f"_cond_elem({block_snapshots[block_id]}, {index_expr}, {default!r})"
    if ctx.blockless:
        ctx.mark_helper("_cond_tag_elem")
        names = ctx.block_name_tuple_symbol(binding.block_id)
        return f"_cond_tag_elem(tags, {names}, {index_expr}, {default!r})"
    ctx.mark_helper("_cond_elem")
    return f"_cond_elem({ctx.symbol_for_block(block)}, {index_expr}, {default!r})"


def _compile_indirect_value(
    indirect_ref: IndirectRef,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
    condition: bool = False,
) -> str:
    binding = ctx.block_bindings.get(id(indirect_ref.block))
    if binding is None:
        raise RuntimeError(f"Missing block binding for indirect ref {indirect_ref.block.name!r}")
    ptr = _compile_value(
        indirect_ref.pointer,
        ctx,
        scalar_snapshots=scalar_snapshots,
        block_snapshots=block_snapshots,
    )
    if condition:
        return _compile_condition_element_read(
            indirect_ref.block, binding, ptr, ctx, block_snapshots
        )
    return _compile_source_element_read(indirect_ref.block, binding, ptr, ctx, block_snapshots)


def _compile_value(
    value: Any,
    ctx: CodegenContext,
    *,
    scalar_snapshots: dict[str, str] | None = None,
    block_snapshots: dict[int, str] | None = None,
    strict: bool = False,
    condition: bool = False,
) -> str:
    """*value* as a Python expression; *strict* for calc (see _compile_expression_impl).

    *condition* for a rung condition's operand: an indexed element outside its
    block reads as 0, with no fault flag.
    """
    if isinstance(value, Tag):
        return _snapshot_tag_symbol(
            value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
    if isinstance(value, IndirectRef):
        return _compile_indirect_value(
            value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            condition=condition,
        )
    if isinstance(value, IndirectExprRef):
        binding = ctx.block_bindings.get(id(value.block))
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression ref {value.block.name!r}"
            )
        expr = _compile_expression_impl(
            value.expr,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
        if condition:
            return _compile_condition_element_read(value.block, binding, expr, ctx, block_snapshots)
        return _compile_source_element_read(value.block, binding, expr, ctx, block_snapshots)
    if isinstance(value, Expression):
        return _compile_expression_impl(
            value,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
            strict=strict,
        )
    return repr(value)
