"""The Math family: math().

laddercodec's Math checks it. The formula holds its tags anywhere in an expression, so the
family overrides ``operands`` to give each tag of the formula.
"""

from __future__ import annotations

from typing import Any, assert_never

from laddercodec import Math

from pyrung.core.expression import (
    Expression,
    LiteralExpr,
    RandIntExpr,
    RangeAggregateExpr,
    SumExpr,
    TagExpr,
)
from pyrung.core.instruction.operands import (
    ConstArg,
    ExprArg,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    Span,
    TagArg,
    Value,
    held,
)
from pyrung.core.memory_block import BlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag, TagType

from ..fields import Family, Operand, Unbuildable, memory_types, refusals_of, render_operand
from ..resolve import _ResolutionCache

# The argument of the pyrung call that holds each field of laddercodec's Math. The tags of
# the formula and the destination set the mode, and the formula holds most of them.
_FIELD_ARGUMENTS: dict[str, int | str] = {
    "Math.expression": 0,
    "Math.dest": 1,
    "Math.mode": 0,
    "Math.oneshot": "oneshot",
}

# pyrung's advice for a code, where the advice names pyrung's own calls or arguments.
_ADVICE: dict[str, str] = {
    "math-nesting-depth": "Break the formula into math() steps that store each part in a tag.",
    "math-formula-too-long": "Break the formula into math() steps that store each part in a tag.",
    "math-arg-expression": "Compute the argument into a tag in a math() above, and pass the tag.",
    "median-range-too-large": (
        "Shrink the median range, or take the median of smaller ranges in separate math() steps."
    ),
    "sum-bank-unavailable": "Copy the values into a bank SUM accepts before summing.",
    "not-writable": "Store the result in a DS register, and copy it on from there if needed.",
    "random-equal-bounds": (
        "Give randint() two different bounds. For two equal constants, write the constant."
    ),
    "pointer-outside-copy": "Copy the value through the pointer with copy() in a rung above.",
}

_BIT_BANKS = frozenset({"X", "Y", "C", "T", "CT", "SC"})


def _formula_leaves(source: Value | Span) -> list[Any]:
    """Return each tag, range and pointer that the formula *source* reads, in order."""
    match source:
        case TagArg(tag=tag):
            return [tag]
        case PointerArg(ref=ref):
            return [ref]
        case RangeArg(span=span):
            return [span]
        case ExprArg(expr=expr):
            return _leaves(expr)
        case ConstArg() | PointerRangeArg():
            return []
        case _:
            assert_never(source)


def _formula_expression(source: Value | Span) -> Any:
    """Return the expression in the formula *source*, or None when the formula is one operand."""
    match source:
        case ExprArg(expr=expr):
            return expr
        case TagArg() | PointerArg() | ConstArg() | RangeArg() | PointerRangeArg():
            return None
        case _:
            assert_never(source)


def _leaves(value: Any, seen: set[int] | None = None) -> list[Any]:
    """Return each tag, range and pointer that the expression *value* reads, in order.

    A part of an expression can be a tag, a pointer, a range or an ``immediate()``.  A
    ``randint()`` bound is one such part.
    """
    seen = set() if seen is None else seen
    if isinstance(value, ImmediateRef):
        value = value.value
    if isinstance(value, (Tag, BlockRange, IndirectRef, IndirectExprRef)):
        return [value]
    if id(value) in seen or not isinstance(value, Expression):
        return []
    seen.add(id(value))
    if isinstance(value, TagExpr):
        return _leaves(value.tag, seen)
    if isinstance(value, (SumExpr, RangeAggregateExpr)):
        return [value.block_range]
    if isinstance(value, RandIntExpr):
        # A draw also holds a hidden tag for its result, which CLICK never sees.
        return [*_leaves(value.lo, seen), *_leaves(value.hi, seen)]
    return [leaf for child in vars(value).values() for leaf in _leaves(child, seen)]


def _operands(instruction: Any) -> list[Operand]:
    """Return the formula, with each tag that it reads, and the destination."""
    found: list[Operand] = []
    leaves = _formula_leaves(instruction.source_expression)
    if leaves:
        found.append(Operand(0, "formula", tuple(("source_expression", leaf) for leaf in leaves)))
    found.append(Operand(1, "destination", (("dest", instruction.dest.tag),)))
    return found


def _bit_operands(source: Value | Span) -> list[Tag]:
    """Return each bit that the formula *source* reads as a plain operand."""
    return [
        leaf
        for leaf in _formula_leaves(source)
        if isinstance(leaf, Tag) and leaf.type is TagType.BOOL
    ]


def _bit_ranges_outside_sum(value: Any, resolver: _ResolutionCache, seen: set[int]) -> bool:
    """Return True if a MAX, MIN, MDN or AVG of the formula *value* reads a bit range."""
    if id(value) in seen or not isinstance(value, Expression):
        return False
    seen.add(id(value))
    if isinstance(value, RangeAggregateExpr):
        return bool(memory_types(value.block_range, resolver) & _BIT_BANKS)
    return any(_bit_ranges_outside_sum(child, resolver, seen) for child in vars(value).values())


def _operand_bank_advice(instruction: Any, resolver: _ResolutionCache) -> str | None:
    """Return the advice for a formula operand in a bank that the Math refuses."""
    bits = _bit_operands(instruction.source_expression)
    if bits:
        # CLICK has no SUM over CT, so a one-bit range cannot stand in for a CT bit.
        if all(memory_types(bit, resolver) <= {"CT"} for bit in bits):
            return (
                "Set a DS tag from the bit's contacts in a rung above (copy(1, Tag) under "
                "the bit, copy(0, Tag) under ~bit) and use the tag."
            )
        return (
            "Write the bit as a one-bit range, c.select(1, 1).sum() for C1 "
            "(SUM ( C1 : C1 )), or set a DS tag from the bit's contacts in a rung above "
            "(copy(1, Tag) under the bit, copy(0, Tag) under ~bit) and use the tag. A one-bit "
            "range of pyrung's sc block reads 0 in pyrung, and a CLICK reads the bit."
        )
    if _bit_ranges_outside_sum(_formula_expression(instruction.source_expression), resolver, set()):
        return "Use .sum() to count the True bits, or copy the bits into registers first."
    if instruction.mode_inference.mixed_families:
        return _SPLIT_ADVICE
    return None


_SPLIT_ADVICE = (
    "Split it into a math() of WORD tags and a math() of the other tags. Pass the result "
    "from one to the other through a tag."
)


def _advice(code: str, field: str, instruction: Any, resolver: _ResolutionCache) -> str | None:
    hex_mode = instruction.mode == "hex"
    if code == "math-operand-bank":
        return _operand_bank_advice(instruction, resolver)
    if code == "bank-not-allowed" and field == "Math.dest":
        if instruction.mode_inference.mixed_families:
            return _SPLIT_ADVICE
        return "Store the result in a register of one of those banks, and copy it on from there."
    if code == "math-function-mode":
        if hex_mode:
            return "Move it into a separate math() of INT, DINT or REAL tags in DS, DD or DF."
        return "Move it into a separate math() of WORD tags in DH."
    if code in ("math-constant-out-of-range", "math-constant-kind"):
        if hex_mode:
            return "Write the constant as a whole 16-bit value, 0..65535 (0h..FFFFh)."
        return "Use a constant in -2147483648..2147483647."
    if code == "random-bound-type" and not hex_mode:
        # laddercodec's own text names the DH register for a hex bound.
        return "Use integer constants, or copy the bound into a DS or DD tag."
    if code == "math-invalid-token" and hex_mode:
        return "Copy the value into a DH, and use that DH in the formula."
    return _ADVICE.get(code)


def _unwritable_hex_constants(value: Any, seen: set[int]) -> list[str]:
    """Return each constant of a hex formula that the export refuses, spelled as CLICK reads it.

    The export refuses a constant outside -32768..65535, a fraction, and a constant whose
    hex spelling CLICK reads as a real number, such as 1Eh. laddercodec refuses each of them
    in these spellings.
    """
    from pyrung.click.ladder.translator import _math_reads_hex_as_real

    if isinstance(value, bool):
        return []
    if isinstance(value, float):
        if not value.is_integer():
            return [repr(value)]
        value = int(value)
    if isinstance(value, int):
        if not -0x8000 <= value <= 0xFFFF:
            return [f"{value & 0xFFFFFFFF if value < 0 else value:X}h"]
        text = f"{value & 0xFFFF:X}h"
        return [text] if _math_reads_hex_as_real(text) else []
    if id(value) in seen or not isinstance(value, Expression):
        return []
    seen.add(id(value))
    if isinstance(value, LiteralExpr):
        children = [value.value]
    elif isinstance(value, RandIntExpr):
        # A draw also holds numbers that are not constants of the formula, such as its site.
        children = [value.lo, value.hi]
    else:
        children = [child for child in vars(value).values() if isinstance(child, Expression)]
    return [text for child in children for text in _unwritable_hex_constants(child, seen)]


def _formula_hex_constants(source: Value | Span) -> list[str]:
    """Return each constant of the hex formula *source* that the export refuses."""
    match source:
        case ConstArg(value=value):
            return _unwritable_hex_constants(value, set())
        case ExprArg(expr=expr):
            return _unwritable_hex_constants(expr, set())
        case TagArg() | PointerArg() | RangeArg() | PointerRangeArg():
            return []
        case _:
            assert_never(source)


def _build(instruction: Any, exporter: Any) -> list[Any]:
    """Return laddercodec's refusals of the Math that the export writes."""
    from pyrung.click.ladder.types import _RenderError

    mode = instruction.mode
    source = instruction.source_expression
    # A hex Math reads every constant as hex, as the export writes it.
    with exporter._hex_constants("math" if mode == "hex" else None):
        try:
            formula = exporter._render_operand(
                held(source),
                path="instruction.expression",
                source=instruction,
            )
        except _RenderError as exc:
            # The export has no spelling for some hex constants. laddercodec refuses each one
            # in the spelling that a user would type into CLICK. The stand-in formula holds
            # only those constants, so it drops the rest of the formula and its faults.
            constants = _formula_hex_constants(source) if mode == "hex" else []
            if not constants:
                # The export refuses the formula and says why.
                raise Unbuildable from exc
            formula = " + ".join(constants)
    dest = render_operand(exporter, instruction.dest.tag, "dest", instruction)
    oneshot = bool(instruction.oneshot)
    refusals = refusals_of(lambda: Math(expression=formula, dest=dest, mode=mode, oneshot=oneshot))
    # laddercodec refuses each place that a tag appears, so a tag used twice gets one finding.
    unique: dict[tuple[str, str, str], Any] = {}
    for refusal in refusals:
        unique.setdefault((refusal.code, refusal.field, refusal.message({})), refusal)
    return list(unique.values())


# The codes of the hex constants that the export refuses even when the code is allowed.
_UNWRITABLE_HEX_CODES = frozenset(
    {"math-constant-out-of-range", "math-constant-kind", "math-invalid-token"}
)


def _export_ignores_allow(code: str, instruction: Any) -> bool:
    """Return True if the export refuses the fault of *code* even when the code is allowed.

    The export has no spelling for a hex constant that CLICK refuses.
    """
    return instruction.mode == "hex" and code in _UNWRITABLE_HEX_CODES


FAMILY = Family(
    name="math",
    classes={"CalcInstruction": {"source_expression": "formula", "dest": "destination"}},
    build=_build,
    arguments=_FIELD_ARGUMENTS,
    advice=_advice,
    operands=_operands,
    export_ignores_allow=_export_ignores_allow,
    replaced_codes={
        "CLK_MATH_MODE_MIXED": "math-operand-bank or bank-not-allowed",
        "CLK_MATH_FUNC_MODE_MISMATCH": "math-function-mode",
        "CLK_MATH_NESTING_DEPTH": "math-nesting-depth",
        "CLK_MATH_MEDIAN_RANGE_TOO_LARGE": "median-range-too-large",
        "CLK_MATH_BIT_RANGE_AGGREGATE": "math-operand-bank",
        "CLK_MATH_SUM_BANK_NOT_AVAILABLE": "sum-bank-unavailable",
        "CLK_MATH_BIT_OPERAND": "math-operand-bank",
        "CLK_MATH_DEST_BANK": "bank-not-allowed",
        "CLK_MATH_ARG_NOT_PLAIN": "math-arg-expression",
        "CLK_MATH_WIDE_CONSTANT": "math-constant-out-of-range",
        "CLK_RAND_BOUND_TYPE": "random-bound-type",
        "CLK_RAND_EQUAL_BOUNDS": "random-equal-bounds",
    },
)
