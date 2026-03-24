"""Expression and operand translation helpers for Click ladder export."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any, NoReturn, cast

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
)
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.expression import (
    AbsExpr,
    AddExpr,
    AndExpr,
    DivExpr,
    ExprCompareEq,
    ExprCompareGe,
    ExprCompareGt,
    ExprCompareLe,
    ExprCompareLt,
    ExprCompareNe,
    Expression,
    FloorDivExpr,
    InvertExpr,
    LiteralExpr,
    LShiftExpr,
    MathFuncExpr,
    ModExpr,
    MulExpr,
    NegExpr,
    OrExpr,
    PosExpr,
    PowExpr,
    RShiftExpr,
    ShiftFuncExpr,
    SubExpr,
    SumExpr,
    TagExpr,
    XorExpr,
)
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.time_mode import TimeUnit

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap, _BlockEntry

# ---- Expression/condition operator maps ----
_BINARY_OP_SYMBOL: dict[type[Expression], str] = {
    AddExpr: " + ",
    SubExpr: " - ",
    MulExpr: " * ",
    DivExpr: " / ",
    FloorDivExpr: "//",
    ModExpr: " MOD ",
    PowExpr: " ^ ",
    AndExpr: " AND ",
    OrExpr: " OR ",
    XorExpr: " XOR ",
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

_UNARY_PREFIX: dict[type[Expression], str] = {
    NegExpr: "-",
    PosExpr: "+",
    InvertExpr: "~",
}

_BINARY_EXPR_TYPES: tuple[type[Expression], ...] = tuple(_BINARY_OP_SYMBOL)

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
    ExprCompareEq: "==",
    ExprCompareNe: "!=",
    ExprCompareLt: "<",
    ExprCompareLe: "<=",
    ExprCompareGt: ">",
    ExprCompareGe: ">=",
}


# ---- Translation mixin ----
class _TranslatorMixin:
    """Render contacts, operands, expressions, and condition fragments."""

    _tag_map: TagMap

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
                message="Immediate edge contacts are not supported in Click ladder export.",
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
                message="Immediate edge contacts are not supported in Click ladder export.",
            )
            return self._fn(
                "fall",
                self._resolve_tag(tag, path=f"{path}.tag", source=condition),
            )
        if isinstance(condition, IntTruthyCondition):
            left = self._resolve_tag(condition.tag, path=f"{path}.tag", source=condition)
            return f"{left}!=0"

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
                left = self._render_indirect_ref(
                    condition.indirect_ref,
                    path=f"{path}.left",
                    source=condition,
                )
                right = self._render_condition_value(
                    condition.value,
                    path=f"{path}.right",
                    source=condition,
                )
                return f"{left}{compare_op}{right}"

            if isinstance(
                condition,
                (
                    ExprCompareEq,
                    ExprCompareNe,
                    ExprCompareLt,
                    ExprCompareLe,
                    ExprCompareGt,
                    ExprCompareGe,
                ),
            ):
                left = self._render_expression(
                    condition.left, path=f"{path}.left", source=condition
                )
                right = self._render_expression(
                    condition.right,
                    path=f"{path}.right",
                    source=condition,
                )
                return f"{left}{compare_op}{right}"

        self._raise_issue(
            path=path,
            message=f"Unsupported condition type: {type(condition).__name__}.",
            source=condition,
        )

    def _explicit_count(
        self,
        *,
        operand: Any,
        configured_count: int | None,
        path: str,
        source: Any,
    ) -> int:
        if configured_count is not None:
            return int(configured_count)
        return self._operand_length(operand, path=path, source=source)

    def _operand_length(self, operand: Any, *, path: str, source: Any) -> int:
        if isinstance(operand, Tag):
            return 1
        if isinstance(operand, BlockRange):
            return len(list(operand.tags()))
        self._raise_issue(
            path=path,
            message=(
                "Automatic count inference is only supported for Tag and BlockRange operands."
            ),
            source=source,
        )

    def _render_condition_value(self, value: Any, *, path: str, source: Any) -> str:
        if isinstance(value, Condition):
            self._raise_issue(
                path=path,
                message="Condition values are not supported in comparisons.",
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
                    message="Immediate wrapper is not supported in this Click export context.",
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
                message="Indirect expression pointers are not supported in Click ladder export.",
                source=source,
            )
        if isinstance(value, IndirectBlockRange):
            self._raise_issue(
                path=path,
                message="Indirect block ranges are not supported in Click ladder export.",
                source=source,
            )
        if isinstance(value, CopyConverter):
            self._raise_issue(
                path=path,
                message="CopyConverter should not appear as a direct operand; use convert= keyword.",
                source=source,
            )
        if isinstance(value, Expression):
            return self._render_expression(value, path=path, source=source)
        if isinstance(value, TimeUnit):
            return value.name
        if isinstance(value, str):
            return _quote(value)
        if isinstance(value, bool):
            return _bool_bit(value)
        if value is None:
            return "none"
        if isinstance(value, (int, float)):
            return repr(value)
        if isinstance(value, tuple | list):
            return self._render_sequence(value, path=path, source=source)
        self._raise_issue(
            path=path,
            message=f"Unsupported operand type: {type(value).__name__}.",
            source=source,
        )

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
                        message="Immediate coil target must resolve to Y bank.",
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
                            message="Immediate coil targets must resolve to Y bank.",
                            source=source,
                        )
                compact = _compact_contiguous_range(addresses)
                # Immediate coil ranges must map to one contiguous Y-bank range.
                compact = self._require_compact_range(
                    compact,
                    path=path,
                    source=source,
                    message=(
                        "Immediate-wrapped coil ranges must map to contiguous "
                        "addresses for Click export."
                    ),
                )
                return self._fn("immediate", compact)

            self._raise_issue(
                path=path,
                message=(
                    "Immediate coil operand must wrap Tag or BlockRange, "
                    f"got {type(wrapped).__name__}."
                ),
                source=source,
            )

        self._raise_issue(
            path=path,
            message=f"Unknown immediate render context: {context!r}.",
            source=source,
        )

    def _render_converter(self, converter: CopyConverter) -> str:
        if converter.mode == "text":
            return self._fn(
                "to_text",
                suppress_zero=_bool_bit(bool(converter.suppress_zero)),
                exponential=_bool_bit(bool(converter.exponential)),
                termination_code="none"
                if converter.termination_code is None
                else f"${converter.termination_code:02X}",
            )
        return f"to_{converter.mode}"

    def _render_expression(self, expression: Expression, *, path: str, source: Any) -> str:
        if isinstance(expression, TagExpr):
            return self._resolve_tag(expression.tag, path=path, source=source)
        if isinstance(expression, LiteralExpr):
            if isinstance(expression.value, bool):
                return _bool_bit(expression.value)
            return repr(expression.value)
        if isinstance(expression, (ShiftFuncExpr, LShiftExpr, RShiftExpr)):
            if isinstance(expression, ShiftFuncExpr):
                click_name = expression.name.upper()
                val = self._render_expression(expression.value, path=f"{path}.value", source=source)
                cnt = self._render_expression(expression.count, path=f"{path}.count", source=source)
            else:
                click_name = "LSH" if isinstance(expression, LShiftExpr) else "RSH"
                val = self._render_expression(expression.left, path=f"{path}.left", source=source)
                cnt = self._render_expression(expression.right, path=f"{path}.right", source=source)
            return self._fn(click_name, val, cnt)
        if isinstance(expression, MathFuncExpr):
            click_name = _MATH_FUNC_CLICK_NAME.get(expression.name, expression.name.upper())
            return self._fn(
                click_name,
                self._render_expression(expression.operand, path=f"{path}.operand", source=source),
            )
        if isinstance(expression, AbsExpr):
            return self._fn(
                "abs",
                self._render_expression(expression.operand, path=f"{path}.operand", source=source),
            )
        if isinstance(expression, (NegExpr, PosExpr, InvertExpr)):
            prefix = _UNARY_PREFIX[type(expression)]
            inner = self._render_expression(
                expression.operand, path=f"{path}.operand", source=source
            )
            if isinstance(expression.operand, _BINARY_EXPR_TYPES):
                return f"{prefix}({inner})"
            return f"{prefix}{inner}"
        if isinstance(
            expression,
            (
                AddExpr,
                SubExpr,
                MulExpr,
                DivExpr,
                FloorDivExpr,
                ModExpr,
                PowExpr,
                AndExpr,
                OrExpr,
                XorExpr,
            ),
        ):
            symbol = _BINARY_OP_SYMBOL[type(expression)]
            left = self._render_expression(expression.left, path=f"{path}.left", source=source)
            right = self._render_expression(expression.right, path=f"{path}.right", source=source)
            # Parenthesize nested binary terms so token rendering is unambiguous.
            if isinstance(expression.left, _BINARY_EXPR_TYPES):
                left = f"({left})"
            if isinstance(expression.right, _BINARY_EXPR_TYPES):
                right = f"({right})"
            return f"{left}{symbol}{right}"
        if isinstance(expression, SumExpr):
            br = expression.block_range
            tags = br.tags()
            first = self._resolve_tag(tags[0], path=f"{path}.block_range[0]", source=source)
            last = self._resolve_tag(tags[-1], path=f"{path}.block_range[-1]", source=source)
            return f"SUM ( {first} : {last} )"
        self._raise_issue(
            path=path,
            message=f"Unsupported expression type: {type(expression).__name__}.",
            source=source,
        )

    def _render_condition_sequence(self, values: tuple[Any, ...], *, path: str, source: Any) -> str:
        rendered: list[str] = []
        for index, value in enumerate(values):
            rendered.append(
                self._render_condition_inline(value, path=f"{path}[{index}]", source=source)
            )
        return f"[{','.join(rendered)}]"

    def _render_condition_inline(self, value: Any, *, path: str, source: Any) -> str:
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
            rendered.append(self._render_operand(value, path=f"{path}[{index}]", source=source))
        return f"[{','.join(rendered)}]"

    def _render_block_range(self, block_range: BlockRange, *, path: str, source: Any) -> str:
        tags = block_range.tags()
        addresses = [
            self._resolve_tag(tag, path=f"{path}[{index}]", source=source)
            for index, tag in enumerate(tags)
        ]
        if not addresses:
            return "[]"
        if len(addresses) == 1:
            return addresses[0]
        compact = _compact_contiguous_range(addresses)
        if compact is not None:
            return compact
        return f"[{','.join(addresses)}]"

    def _render_indirect_ref(self, indirect: IndirectRef, *, path: str, source: Any) -> str:
        entry = self._require_block_entry(indirect.block.name, path=path, source=source)

        try:
            offset = self._tag_map.offset_for(entry.logical)
        except Exception:
            self._raise_issue(
                path=path,
                message=(
                    f"Indirect block {indirect.block.name!r} must have an affine mapping "
                    "for Click ladder export."
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
        parsed_address = cast(tuple[str, int], parsed_hardware)
        bank, _ = parsed_address
        pointer = self._resolve_tag(indirect.pointer, path=f"{path}.pointer", source=source)
        if offset == 0:
            return f"{bank}[{pointer}]"
        sign = "+" if offset > 0 else "-"
        return f"{bank}[{pointer}{sign}{abs(offset)}]"

    def _require_block_entry(self, block_name: str, *, path: str, source: Any) -> _BlockEntry:
        entry: _BlockEntry | None = self._tag_map.block_entry_by_name(block_name)
        if entry is None:
            self._raise_issue(
                path=path,
                message=f"Indirect block {block_name!r} is not mapped in TagMap.",
                source=source,
            )
        return cast("_BlockEntry", entry)

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
        return cast(str, compact)

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
            self._raise_issue(
                path=path,
                message=f"Tag {tag.name!r} is not mapped in TagMap.",
                source=source,
            )


# ---- String/render utilities ----
def _quote(value: str) -> str:
    escaped = value.replace('"', '""')
    return f'"{escaped}"'


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
    return match.group(1), int(match.group(2))


__all__ = [
    "_BINARY_OP_SYMBOL",
    "_COMPARE_OPS",
    "_TranslatorMixin",
    "_UNARY_PREFIX",
    "_bool_bit",
    "_compact_contiguous_range",
    "_parse_display_address",
    "_quote",
]
