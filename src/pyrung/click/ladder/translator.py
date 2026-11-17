"""Expression and operand translation helpers for Click ladder export."""

from __future__ import annotations

import math
import re
import struct
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Literal, NoReturn, TypeGuard

from pyclickplc.banks import BANKS

from pyrung.click.capabilities import HEX_CONSTANT_BANKS
from pyrung.click.tag_map._map import unmapped_tag_text
from pyrung.click.validation.findings import (
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    _bank_pointer_advice,
    _build_suggestion,
    immediate_coil_not_y_text,
    immediate_coil_operand_text,
    immediate_coil_range_gap_text,
    immediate_edge_text,
    immediate_misplaced_text,
    pointer_block_range_text,
    pointer_expression_text,
    pointer_offset_text,
)
from pyrung.core.condition import (
    AllCondition,
    AnyCondition,
    BitCondition,
    CompareEq,
    CompareGe,
    CompareGt,
    CompareLe,
    CompareLt,
    CompareNe,
    Condition,
    FallingEdgeCondition,
    IndirectCompareEq,
    IndirectCompareGe,
    IndirectCompareGt,
    IndirectCompareLe,
    IndirectCompareLt,
    IndirectCompareNe,
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
    text_window_of,
)
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.expression import (
    BinaryExpr,
    ExprCompare,
    Expression,
    LiteralExpr,
    MathFuncExpr,
    RandIntExpr,
    RangeAggregateExpr,
    ShiftFuncExpr,
    SumExpr,
    TagExpr,
    UnaryExpr,
)
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.storage import BLANK_CHAR
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap

# Pre-built Click bank names that can be used directly as hardware prefixes
# in indirect addressing without an explicit TagMap block entry.
_CLICK_BANK_NAMES: frozenset[str] = frozenset(BANKS)

#: CLICK's ASCII-code constant for the TXT blank (NUL).  ``""`` writes no TXT.
BLANK_TEXT_CONSTANT = "$00"

# ---- Expression/condition operator maps ----
# Maps Python operator symbol → Click formula-pad symbol for binary expressions
_BINARY_OP_CLICK: dict[str, str] = {
    "+": " + ",
    "-": " - ",
    "*": " * ",
    "/": " / ",
    "//": "//",
    "%": " MOD ",
    "**": " ^ ",
    "&": " AND ",
    "|": " OR ",
    "^": " XOR ",
}

# Python math-function name → Click formula-pad name
_MATH_FUNC_CLICK_NAME: dict[str, str] = {
    "sqrt": "SQRT",
    "sin": "SIN",
    "cos": "COS",
    "tan": "TAN",
    "asin": "ASIN",
    "acos": "ACOS",
    "atan": "ATAN",
    "radians": "RAD",
    "degrees": "DEG",
    "log10": "LOG",
    "log": "LN",
}

#: Range aggregates: pyrung method name -> CLICK formula function.
_RANGE_AGGREGATE_CLICK_NAME: dict[str, str] = {
    "sum": "SUM",
    "max": "MAX",
    "min": "MIN",
    "median": "MDN",
    "mean": "AVG",
}


def _formula_call(name: str, *args: str) -> str:
    """Return CLICK's formula spelling ``NAME ( a , b )``.

    There are spaces around each paren and separator.  CLICK splits a formula's
    internal form on these spaces (``ABS( x )`` is a Math instruction error in
    CLICK v4).  A copy-back decodes to this spelling.
    """
    return f"{name} ( {' , '.join(args)} )"


_COMPARE_OPS: dict[type[Condition], str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
    IndirectCompareEq: "==",
    IndirectCompareNe: "!=",
    IndirectCompareLt: "<",
    IndirectCompareLe: "<=",
    IndirectCompareGt: ">",
    IndirectCompareGe: ">=",
}


# ---- Translation mixin ----
class _TranslatorMixin:
    """Render contacts, operands, expressions, and condition fragments."""

    def _with_advice(self, message: str, code: str) -> str:
        """Return *message* followed by the advice that portability gives for *code*."""
        advice = _build_suggestion(code, None, self._tag_map)
        return f"{message} {advice}" if advice else message

    _tag_map: TagMap

    # How integer constants are spelled right now (see _hex_constants).
    # - None: decimal.
    # - "word": hex, for a Copy, Fill or Math into a Word, with -32768..-1 written as its
    #   16 bits.  Any other value outside 0..FFFFh is an export error, since no CLICK hex
    #   constant holds it.
    # - "exact": hex.  A value outside 0..FFFFh is an export error.
    _hex_constant_mode: Literal["word", "exact"] | None = None

    @contextmanager
    def _hex_constants(self, mode: Literal["word", "exact"] | None) -> Iterator[None]:
        """Spell the integer constants rendered inside the block in CLICK hex.

        CLICK reads digits typed into a hex context as hex and appends ``h``.
        Typing ``255`` gives ``255h``, which is 0x255.  A pasted rung gets no
        such fix-up, so a bare decimal there is an error.  pyrung literals stay
        decimal, so pyrung ``255`` exports as ``FFh``.
        """
        previous = self._hex_constant_mode
        self._hex_constant_mode = mode
        try:
            yield
        finally:
            self._hex_constant_mode = previous

    @staticmethod
    def _is_hex_bank_operand(rendered: str) -> bool:
        """Return True when a rendered operand is in a hex bank.

        ``DH1``, ``DH1..DH4`` and ``DH[DS1]`` are such operands.
        """
        match = re.match(r"[A-Za-z]+", rendered)
        return match is not None and match.group(0).upper() in HEX_CONSTANT_BANKS

    def _render_number(self, value: int | float, *, path: str, source: Any) -> str:
        """Spell an int or float constant the way CLICK writes it."""
        mode = self._hex_constant_mode
        if mode is not None:
            if isinstance(value, float):
                if not value.is_integer():
                    self._raise_issue(
                        path=path,
                        message=(
                            f"Constant {value!r} is not an integer; CLICK hex constants "
                            "are whole 16-bit values (0h..FFFFh)."
                        ),
                        source=source,
                    )
                value = int(value)
            if mode == "exact" and not 0 <= value <= 0xFFFF:
                self._raise_issue(
                    path=path,
                    message=(
                        f"Constant {value} is outside 0..65535; CLICK hex constants "
                        "are 0h..FFFFh, so this compare cannot be written in CLICK."
                    ),
                    source=source,
                )
            if mode == "word" and not -0x8000 <= value <= 0xFFFF:
                self._raise_issue(
                    path=path,
                    message=(
                        f"Constant {value} does not fit in 16 bits. A CLICK hex constant is "
                        "0h..FFFFh, so this cannot be written in CLICK."
                    ),
                    source=source,
                )
            # Write it unpadded, as CLICK's Math normalizes it (000Ah -> Ah).  Copy and
            # Compare keep either spelling.
            return f"{value & 0xFFFF:X}h"
        if isinstance(value, float):
            text = click_real_literal(value)
            if text is None:
                self._raise_issue(
                    path=path,
                    message=(
                        f"REAL constant {value!r} is not representable in CLICK "
                        "(finite values within +/-3.4028235E+38 only)."
                    ),
                    source=source,
                )
            return text
        return str(value)

    if TYPE_CHECKING:

        def _fn(self, name: str, *args: str, **kwargs: str) -> str: ...
        def _raise_issue(self, *, path: str, message: str, source: Any) -> NoReturn: ...

    def _condition_leaf_token(self, condition: Condition, *, path: str) -> str:
        if isinstance(condition, BitCondition):
            return self._render_contact_token(condition.tag, path=f"{path}.tag", source=condition)
        if isinstance(condition, NormallyClosedCondition):
            token = self._render_contact_token(condition.tag, path=f"{path}.tag", source=condition)
            return f"~{token}"
        if isinstance(condition, RisingEdgeCondition):
            tag = self._require_non_immediate_tag(
                condition.tag,
                path=f"{path}.tag",
                source=condition,
                message=self._with_advice(
                    immediate_edge_text(), CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED
                ),
            )
            return self._fn(
                "rise",
                self._resolve_tag(tag, path=f"{path}.tag", source=condition),
            )
        if isinstance(condition, FallingEdgeCondition):
            tag = self._require_non_immediate_tag(
                condition.tag,
                path=f"{path}.tag",
                source=condition,
                message=self._with_advice(
                    immediate_edge_text(), CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED
                ),
            )
            return self._fn(
                "fall",
                self._resolve_tag(tag, path=f"{path}.tag", source=condition),
            )
        if isinstance(condition, IntTruthyCondition):
            left = self._resolve_tag(condition.tag, path=f"{path}.tag", source=condition)
            zero = "0h" if self._is_hex_bank_operand(left) else "0"
            return f"{left}!={zero}"

        if isinstance(condition, ExprCompare):
            left = self._render_expression(condition.left, path=f"{path}.left", source=condition)
            with self._hex_constants("exact" if self._is_hex_bank_operand(left) else None):
                right = self._render_expression(
                    condition.right,
                    path=f"{path}.right",
                    source=condition,
                )
            return f"{left}{condition.symbol}{right}"

        compare_op = _COMPARE_OPS.get(type(condition))
        if compare_op is not None:
            if isinstance(
                condition,
                (
                    CompareEq,
                    CompareNe,
                    CompareLt,
                    CompareLe,
                    CompareGt,
                    CompareGe,
                ),
            ):
                left = self._resolve_tag(condition.tag, path=f"{path}.left", source=condition)
                with self._hex_constants("exact" if self._is_hex_bank_operand(left) else None):
                    right = self._render_condition_value(
                        condition.value,
                        path=f"{path}.right",
                        source=condition,
                    )
                return f"{left}{compare_op}{right}"

            if isinstance(
                condition,
                (
                    IndirectCompareEq,
                    IndirectCompareNe,
                    IndirectCompareLt,
                    IndirectCompareLe,
                    IndirectCompareGt,
                    IndirectCompareGe,
                ),
            ):
                # ``_render_operand`` renders ``block[pointer]`` and rejects
                # ``block[expr]``, which CLICK cannot address.
                left = self._render_operand(
                    condition.indirect_ref,
                    path=f"{path}.left",
                    source=condition,
                )
                with self._hex_constants("exact" if self._is_hex_bank_operand(left) else None):
                    right = self._render_condition_value(
                        condition.value,
                        path=f"{path}.right",
                        source=condition,
                    )
                return f"{left}{compare_op}{right}"

        self._raise_issue(
            path=path,
            message=f"Unsupported condition type: {type(condition).__name__}.",
            source=condition,
        )

    def _render_condition_value(self, value: Any, *, path: str, source: Any) -> str:
        if isinstance(value, Condition):
            self._raise_issue(
                path=path,
                message=(
                    "One side of this comparison is a condition. Compare against a tag or a "
                    "constant."
                ),
                source=source,
            )
        return self._render_operand(value, path=path, source=source)

    def _render_operand(
        self,
        value: Any,
        *,
        path: str,
        source: Any,
        allow_immediate: bool = False,
        immediate_context: str = "",
    ) -> str:
        if isinstance(value, ImmediateRef):
            if not allow_immediate:
                self._raise_issue(
                    path=path,
                    message=self._with_advice(
                        immediate_misplaced_text(), CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED
                    ),
                    source=source,
                )
            return self._render_immediate_operand(
                value,
                path=path,
                source=source,
                context=immediate_context,
            )
        if isinstance(value, Tag):
            return self._resolve_tag(value, path=path, source=source)
        if isinstance(value, BlockRange):
            return self._render_block_range(value, path=path, source=source)
        if isinstance(value, IndirectRef):
            return self._render_indirect_ref(value, path=path, source=source)
        if isinstance(value, IndirectExprRef):
            self._raise_issue(
                path=path,
                message=self._with_advice(pointer_expression_text(), CLK_PTR_EXPR_NOT_ALLOWED),
                source=source,
            )
        if isinstance(value, IndirectBlockRange):
            self._raise_issue(
                path=path,
                message=self._with_advice(
                    pointer_block_range_text(), CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED
                ),
                source=source,
            )
        if isinstance(value, CopyConverter):
            self._raise_issue(
                path=path,
                message=(
                    "A converter such as to_text() is an operand here. Pass it as the "
                    "convert= argument of copy()."
                ),
                source=source,
            )
        if isinstance(value, Expression):
            return self._render_expression(value, path=path, source=source)
        if isinstance(value, TimeUnit):
            return value.name
        if isinstance(value, str):
            return self._render_text(value, path=path, source=source)
        if isinstance(value, bool):
            return _bool_bit(value)
        if value is None:
            return "none"
        if isinstance(value, (int, float)):
            return self._render_number(value, path=path, source=source)
        if isinstance(value, tuple | list):
            return self._render_sequence(value, path=path, source=source)
        self._raise_issue(
            path=path,
            message=f"Unsupported operand type: {type(value).__name__}.",
            source=source,
        )

    def _render_text(self, value: str, *, path: str, source: Any) -> str:
        """Return a text constant as CLICK spells it.  The blank character is ``$00``.

        A TXT blank is NUL.  A quoted CLICK text constant writes one TXT per
        character, so ``""`` writes nothing (and compares equal to anything).
        ``$00``, CLICK's ASCII-code constant, is the one-character NUL.  A raw
        NUL inside quotes would end the constant early.  pyrung's ``""`` is the
        blank too (a CHAR stores it as NUL), so it exports the same way.
        """
        if value in ("", BLANK_CHAR):
            return BLANK_TEXT_CONSTANT
        if BLANK_CHAR in value:
            self._raise_issue(
                path=path,
                message=(
                    f"Text constant {value!r} holds a NUL, which ends a CLICK text "
                    "constant. Write the blank character on its own (it exports "
                    f"as {BLANK_TEXT_CONSTANT})."
                ),
                source=source,
            )
        return _quote(value)

    def _render_contact_token(self, value: Any, *, path: str, source: Any) -> str:
        return self._render_operand(
            value,
            path=path,
            source=source,
            allow_immediate=True,
            immediate_context="contact",
        )

    def _render_immediate_operand(
        self,
        immediate_ref: ImmediateRef,
        *,
        path: str,
        source: Any,
        context: str,
    ) -> str:
        wrapped = immediate_ref.value

        if context == "contact":
            tag = self._require_tag(
                wrapped,
                path=path,
                source=source,
                message="Immediate contact requires a Tag operand.",
            )
            return self._fn(
                "immediate",
                self._resolve_tag(tag, path=f"{path}.value", source=source),
            )

        if context == "coil":
            if isinstance(wrapped, Tag):
                address = self._resolve_tag(wrapped, path=f"{path}.value", source=source)
                parsed = _parse_display_address(address)
                if parsed is None or parsed[0] != "Y":
                    self._raise_issue(
                        path=path,
                        message=self._with_advice(
                            immediate_coil_not_y_text(found=address),
                            CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
                        ),
                        source=source,
                    )
                return self._fn("immediate", address)

            if isinstance(wrapped, BlockRange):
                tags = wrapped.tags()
                addresses = [
                    self._resolve_tag(tag, path=f"{path}.value[{idx}]", source=source)
                    for idx, tag in enumerate(tags)
                ]
                if not addresses:
                    self._raise_issue(
                        path=path,
                        message="Immediate coil range cannot be empty.",
                        source=source,
                    )
                for address in addresses:
                    parsed = _parse_display_address(address)
                    if parsed is None or parsed[0] != "Y":
                        self._raise_issue(
                            path=path,
                            message=self._with_advice(
                                immediate_coil_not_y_text(found=address),
                                CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
                            ),
                            source=source,
                        )
                compact = _compact_contiguous_range(addresses)
                # Immediate coil ranges must map to one contiguous Y-bank range.
                compact = self._require_compact_range(
                    compact,
                    path=path,
                    source=source,
                    message=self._with_advice(
                        immediate_coil_range_gap_text(), CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS
                    ),
                )
                return self._fn("immediate", compact)

            self._raise_issue(
                path=path,
                message=immediate_coil_operand_text(type(wrapped).__name__),
                source=source,
            )

        self._raise_issue(
            path=path,
            message=f"Unknown immediate render context: {context!r}.",
            source=source,
        )

    def _render_converter(
        self, converter: CopyConverter, instruction: Any, source_text: str, *, path: str
    ) -> str:
        if converter.mode == "text":
            # CLICK offers Exponential Numbering for a Real source and Suppress Zero for
            # an integer one, so only that one flag is written.
            if _operand_type(instruction.source) is TagType.REAL:
                flag = {"exponential": _bool_bit(bool(converter.exponential))}
            elif converter.exponential:
                self._raise_issue(
                    path=path,
                    message=(
                        "to_text(exponential=True) needs a Real source, such as a DF or a "
                        f"Real constant. The source here is {source_text}. Remove "
                        "exponential=True, or use suppress_zero for an integer source."
                    ),
                    source=instruction,
                )
            else:
                flag = {"suppress_zero": _bool_bit(bool(converter.suppress_zero))}
            return self._fn(
                "to_text",
                **flag,
                termination_code="none"
                if converter.termination_code is None
                else f"${converter.termination_code:02X}",
            )
        return f"to_{converter.mode}"

    def _render_zero_minus(self, negation: UnaryExpr, *, path: str, source: Any) -> str:
        """Return ``-DS2`` inside a formula as ``( 0 - DS2 )``.

        CLICK accepts a leading ``- DS1``.  It refuses a minus on an operand anywhere
        later, even when typed (``DS1 * ( - DS2 )``).  ``DS1 * ( 0 - DS2 )`` passes.
        """
        operand = negation.operand
        inner = self._render_expression(operand, path=f"{path}.operand", source=source)
        if isinstance(operand, BinaryExpr):
            inner = f"( {inner} )"
        zero = self._render_number(0, path=path, source=source)
        return f"( {zero} - {inner} )"

    def _render_expression(self, expression: Expression, *, path: str, source: Any) -> str:
        if isinstance(expression, TagExpr):
            return self._resolve_tag(expression.tag, path=path, source=source)
        if isinstance(expression, LiteralExpr):
            if isinstance(expression.value, bool):
                return _bool_bit(expression.value)
            if isinstance(expression.value, (int, float)):
                return self._render_number(expression.value, path=path, source=source)
            if isinstance(expression.value, str):
                return self._render_text(expression.value, path=path, source=source)
            return repr(expression.value)
        if isinstance(expression, RandIntExpr):
            lo = self._render_expression(expression.lo, path=f"{path}.lo", source=source)
            hi = self._render_expression(expression.hi, path=f"{path}.hi", source=source)
            return _formula_call("RAND", lo, hi)
        if isinstance(expression, ShiftFuncExpr):
            click_name = expression.name.upper()
            val = self._render_expression(expression.value, path=f"{path}.value", source=source)
            cnt = self._render_expression(expression.count, path=f"{path}.count", source=source)
            return _formula_call(click_name, val, cnt)
        if isinstance(expression, MathFuncExpr):
            click_name = _MATH_FUNC_CLICK_NAME.get(expression.name, expression.name.upper())
            return _formula_call(
                click_name,
                self._render_expression(expression.operand, path=f"{path}.operand", source=source),
            )
        if isinstance(expression, UnaryExpr):
            inner = self._render_expression(
                expression.operand, path=f"{path}.operand", source=source
            )
            if expression.symbol == "abs":
                return _formula_call("ABS", inner)
            if isinstance(expression.operand, BinaryExpr):
                return f"{expression.symbol} ( {inner} )"
            if expression.symbol == "-":
                # CLICK's editor redisplays -DS1 as - DS1.
                return f"- {inner}"
            return f"{expression.symbol}{inner}"
        if isinstance(expression, BinaryExpr):
            # << and >> map to Click shift functions
            if expression.symbol in ("<<", ">>"):
                click_name = "LSH" if expression.symbol == "<<" else "RSH"
                val = self._render_expression(expression.left, path=f"{path}.left", source=source)
                cnt = self._render_expression(expression.right, path=f"{path}.right", source=source)
                return _formula_call(click_name, val, cnt)
            symbol = _BINARY_OP_CLICK[expression.symbol]
            left = self._render_expression(expression.left, path=f"{path}.left", source=source)
            right = self._render_expression(expression.right, path=f"{path}.right", source=source)
            if _is_negation(expression.left):
                left = self._render_zero_minus(expression.left, path=f"{path}.left", source=source)
            elif isinstance(expression.left, BinaryExpr):
                left = f"( {left} )"
            if _is_negation(expression.right):
                right = self._render_zero_minus(
                    expression.right, path=f"{path}.right", source=source
                )
            elif isinstance(expression.right, BinaryExpr) or right.startswith("-"):
                # CLICK refuses a sign straight after an operator (DS1 + -5),
                # but takes it in parentheses (DS1 + ( -5 )).
                right = f"( {right} )"
            return f"{left}{symbol}{right}"
        if isinstance(expression, SumExpr):
            return self._render_range_aggregate("sum", expression.block_range, path, source)
        if isinstance(expression, RangeAggregateExpr):
            return self._render_range_aggregate(
                expression.kind, expression.block_range, path, source
            )
        self._raise_issue(
            path=path,
            message=f"Unsupported expression type: {type(expression).__name__}.",
            source=source,
        )

    def _render_range_aggregate(self, kind: str, br: Any, path: str, source: Any) -> str:
        """Return ``SUM ( DS1 : DS10 )`` or its MAX, MIN, MDN or AVG form.  Bit banks pass as-is."""
        tags = br.tags()
        first = self._resolve_tag(tags[0], path=f"{path}.block_range[0]", source=source)
        last = self._resolve_tag(tags[-1], path=f"{path}.block_range[-1]", source=source)
        return _formula_call(_RANGE_AGGREGATE_CLICK_NAME[kind], f"{first} : {last}")

    def _render_condition_sequence(self, values: tuple[Any, ...], *, path: str, source: Any) -> str:
        rendered: list[str] = []
        for index, value in enumerate(values):
            if value is None:
                rendered.append("None")
                continue
            rendered.append(
                self._render_condition_inline(value, path=f"{path}[{index}]", source=source)
            )
        return f"[{','.join(rendered)}]"

    def _render_condition_inline(self, value: Any, *, path: str, source: Any) -> str:
        if isinstance(value, Condition):
            value = text_window_of(value)
        if isinstance(value, AllCondition):
            return self._fn(
                "all",
                *(
                    self._render_condition_inline(c, path=path, source=source)
                    for c in value.conditions
                ),
            )
        if isinstance(value, AnyCondition):
            return self._fn(
                "any",
                *(
                    self._render_condition_inline(c, path=path, source=source)
                    for c in value.conditions
                ),
            )
        if isinstance(value, Condition):
            return self._condition_leaf_token(value, path=path)
        self._raise_issue(
            path=path,
            message=f"Expected condition, got {type(value).__name__}.",
            source=source,
        )

    def _render_pattern(self, pattern: tuple[tuple[bool, ...], ...]) -> str:
        rows: list[str] = []
        for row in pattern:
            rows.append(f"[{','.join(_bool_bit(cell) for cell in row)}]")
        return f"[{','.join(rows)}]"

    def _render_sequence(self, values: Any, *, path: str, source: Any) -> str:
        rendered: list[str] = []
        for index, value in enumerate(values):
            if value is None:
                rendered.append("None")
                continue
            rendered.append(self._render_operand(value, path=f"{path}[{index}]", source=source))
        return f"[{','.join(rendered)}]"

    def _render_shift_range(self, bit_range: Any, *, path: str, source: Any) -> str:
        """Return a shift range.  A reversed one is ``C1018..C1011`` (end below start).

        CLICK stores the shift register's start and end as separate fields and
        shifts from start toward end.  An ending address lower than the
        starting one therefore shifts downward.  pyrung spells that ``.reverse()``.
        """
        if isinstance(bit_range, BlockRange):
            addresses = [
                self._resolve_tag(tag, path=f"{path}[{index}]", source=source)
                for index, tag in enumerate(bit_range.tags())
            ]
            descending = _compact_contiguous_range(addresses[::-1])
            if len(addresses) > 1 and descending is not None:
                return f"{addresses[0]}..{addresses[-1]}"
        return self._render_operand(bit_range, path=path, source=source)

    def _render_block_range(self, block_range: BlockRange, *, path: str, source: Any) -> str:
        tags = block_range.tags()
        addresses = [
            self._resolve_tag(tag, path=f"{path}[{index}]", source=source)
            for index, tag in enumerate(tags)
        ]
        if not addresses:
            return "[]"
        compact = _compact_contiguous_range(addresses)
        if compact is not None:
            return compact
        return f"[{','.join(addresses)}]"

    def _render_indirect_ref(self, indirect: IndirectRef, *, path: str, source: Any) -> str:
        entry = self._tag_map._block_entry_by_name(indirect.block.name)

        if entry is not None:
            try:
                offset = self._tag_map._offset_for(entry.logical)
            except Exception:
                self._raise_issue(
                    path=path,
                    message=(
                        f"A pointer cannot address block {indirect.block.name!r} in CLICK, "
                        "because its slots do not map to consecutive CLICK addresses. Map the "
                        "block to one unbroken CLICK range."
                    ),
                    source=source,
                )

            sample_logical = entry.logical_addresses[0]
            hardware_addr = self._tag_map.resolve(entry.logical, sample_logical)
            parsed_hardware = _parse_display_address(hardware_addr)
            if not isinstance(parsed_hardware, tuple):
                self._raise_issue(
                    path=path,
                    message=f"Unable to parse hardware bank from {hardware_addr!r}.",
                    source=source,
                )
            bank, _ = parsed_hardware
        else:
            # Unmapped block — accept pre-built Click banks (DS, DD, DH, …)
            # directly.  Their block name IS the hardware bank prefix and
            # the addressing is identity (offset 0).
            if indirect.block.name not in _CLICK_BANK_NAMES:
                self._raise_issue(
                    path=path,
                    message=(
                        f"Block {indirect.block.name!r} has no CLICK address. Map it in your "
                        "TagMap."
                    ),
                    source=source,
                )
            bank = indirect.block.name
            offset = 0

        pointer = self._resolve_tag(indirect.pointer, path=f"{path}.pointer", source=source)
        if offset != 0:
            advice = _bank_pointer_advice(
                indirect.block.name,
                indirect.pointer.name,
                self._tag_map,
                dest=path.endswith((".dest", ".target")),
            )
            self._raise_issue(
                path=path,
                message=(
                    "CLK_PTR_BLOCK_OFFSET: "
                    f"{pointer_offset_text(indirect.block.name, indirect.pointer.name, offset)} "
                    f"{advice}"
                ),
                source=source,
            )
        return f"{bank}[{pointer}]"

    def _require_compact_range(
        self,
        compact: str | None,
        *,
        path: str,
        source: Any,
        message: str,
    ) -> str:
        if not isinstance(compact, str):
            self._raise_issue(path=path, message=message, source=source)
        return compact

    def _require_tag(self, value: Any, *, path: str, source: Any, message: str) -> Tag:
        if not isinstance(value, Tag):
            self._raise_issue(path=path, message=message, source=source)
        return value

    def _require_non_immediate_tag(
        self,
        value: Tag | ImmediateRef,
        *,
        path: str,
        source: Any,
        message: str,
    ) -> Tag:
        if isinstance(value, ImmediateRef):
            self._raise_issue(path=path, message=message, source=source)
        return self._require_tag(value, path=path, source=source, message=message)

    def _resolve_tag(self, tag: Tag, *, path: str, source: Any) -> str:
        try:
            return self._tag_map.resolve(tag)
        except Exception:
            block = getattr(tag, "_pyrung_block", None)
            if block is not None:
                addr: int = tag._pyrung_block_addr  # ty: ignore[unresolved-attribute]
                return block._format_tag_name(addr)
            if _parse_display_address(tag.name) is not None:
                return tag.name
            self._raise_issue(
                path=path,
                message=unmapped_tag_text(tag.name),
                source=source,
            )


# ---- String/render utilities ----
def _is_negation(expression: Expression) -> TypeGuard[UnaryExpr]:
    """Return True for a unary minus on an operand (``-DS1``, ``-(DS1 + 1)``).

    A negative literal is not one.
    """
    return isinstance(expression, UnaryExpr) and expression.symbol == "-"


#: Significant digits CLICK keeps of a REAL constant.
_CLICK_REAL_DIGITS = 7


def _float32(value: float) -> float | None:
    """Return *value* rounded to float32, or None when it overflows float32."""
    try:
        return struct.unpack("<f", struct.pack("<f", value))[0]
    except OverflowError:
        return None


def click_real_literal(value: float) -> str | None:
    """Return CLICK's spelling of a REAL constant, or None when CLICK cannot hold it.

    CLICK stores REALs as float32 but keeps at most 7 significant digits of a
    constant.  A pasted ``3.40282347E+38`` comes back ``3.402823e+38``.  So the
    digits are the shortest that read back to the same float32, capped at 7.
    The cap also keeps the REAL maximum in range.  Its shortest spelling,
    ``3.4028235E+38``, is past float32's largest value as written, and CLICK
    v4's syntax check rejects it.  A value that needs 8 or 9 digits (``1 / 3``)
    is written as the 7-digit constant that CLICK will hold.

    Magnitudes that Python writes positionally stay positional (``3.14``,
    ``100.0``).  The rest use CLICK's exponent form with a decimal point and an
    uppercase ``E`` (``3.402823E+38``, ``1.0E-10``).

    From 1e7 up, CLICK saves the E form (12345678.0 saves as 1.234568E+07).
    This function does not write that yet.  The roadmap holds that change.
    """
    if not math.isfinite(value):
        return None
    f32 = _float32(value)
    if f32 is None:
        return None
    if f32 == 0:
        return "0.0"
    digits = repr(f32)
    for precision in range(1, _CLICK_REAL_DIGITS + 1):
        digits = f"{f32:.{precision - 1}e}"
        if _float32(float(digits)) == f32:
            break
    mantissa, exponent_text = digits.split("e")
    exponent = int(exponent_text)
    if -4 <= exponent < 16:
        return repr(float(digits))
    if "." not in mantissa:
        mantissa += ".0"
    return f"{mantissa}E{'+' if exponent >= 0 else '-'}{abs(exponent):02d}"


def _quote(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


def _operand_type(operand: Any) -> TagType | None:
    """Return the tag type of a copy operand.

    A pointer has its block's type.  A float constant counts as a Real.
    """
    if isinstance(operand, ImmediateRef):
        operand = operand.value
    if isinstance(operand, Tag):
        return operand.type
    if isinstance(operand, (BlockRange, IndirectBlockRange, IndirectRef, IndirectExprRef)):
        return operand.block.type
    if isinstance(operand, float):
        return TagType.REAL
    return None


def _bool_bit(value: bool) -> str:
    return "1" if bool(value) else "0"


def _compact_contiguous_range(addresses: list[str]) -> str | None:
    parsed = [_parse_display_address(value) for value in addresses]
    if any(item is None for item in parsed):
        return None

    assert all(item is not None for item in parsed)
    banks = {item[0] for item in parsed if item is not None}
    if len(banks) != 1:
        return None

    nums = [item[1] for item in parsed if item is not None]
    if any(nums[idx] + 1 != nums[idx + 1] for idx in range(len(nums) - 1)):
        return None

    return f"{addresses[0]}..{addresses[-1]}"


def _parse_display_address(value: str) -> tuple[str, int] | None:
    match = re.fullmatch(r"([A-Za-z]+)(\d+)", value)
    if match is None:
        return None
    bank = match.group(1)
    if bank.upper() not in _CLICK_BANK_NAMES:
        return None
    return bank, int(match.group(2))


__all__ = [
    "_BINARY_OP_CLICK",
    "_COMPARE_OPS",
    "_TranslatorMixin",
    "_bool_bit",
    "_compact_contiguous_range",
    "_parse_display_address",
    "_quote",
]
