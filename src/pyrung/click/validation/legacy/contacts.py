"""pyrung's checks of a rung's contacts and comparisons."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pyrung.core.condition import Condition
from pyrung.core.instruction.control import iter_loop_body_rungs
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation._common import rung_label
from pyrung.core.validation.walker import ProgramLocation

from ..checks import Check
from ..findings import ClickFinding, ValidationMode, _route_severity
from ..resolve import (
    _bank_label,
    _format_location,
    _ResolutionCache,
)

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap


CLK_IMMEDIATE_CONTACT_MUST_BE_X = "CLK_IMMEDIATE_CONTACT_MUST_BE_X"
CLK_TEXT_ORDER_COMPARE = "CLK_TEXT_ORDER_COMPARE"
CLK_COMPARE_HEX_MIX = "CLK_COMPARE_HEX_MIX"
CODES = (CLK_IMMEDIATE_CONTACT_MUST_BE_X, CLK_TEXT_ORDER_COMPARE, CLK_COMPARE_HEX_MIX)


def _evaluate_immediate_contacts(
    program: Any, tag_map: TagMap, mode: ValidationMode, resolver: _ResolutionCache
) -> list[ClickFinding]:
    """Report each immediate NO/NC contact whose tag is mapped to a bank other than X.

    CLICK offers an immediate contact on X only.
    """
    from pyrung.core.condition import (
        AllCondition,
        AnyCondition,
        BitCondition,
        NormallyClosedCondition,
    )

    findings: list[ClickFinding] = []

    def contacts(condition: Any, out: list[Tag]) -> None:
        if isinstance(condition, (AllCondition, AnyCondition)):
            for child in condition.conditions:
                contacts(child, out)
        elif isinstance(condition, (BitCondition, NormallyClosedCondition)) and isinstance(
            condition.tag, ImmediateRef
        ):
            out.append(condition._resolved_tag)

    def walk(rung: Any, location: ProgramLocation, *, loops: bool = True) -> None:
        # iter_loop_body_rungs also yields the rungs of nested loops.  Walk those with
        # loops=False.
        wrapped: list[Tag] = []
        for condition in rung._conditions[rung._branch_condition_start :]:
            contacts(condition, wrapped)
        location_text = _format_location(location)
        for tag in wrapped:
            slot = resolver.resolve_direct_tag(tag)
            if slot is None or slot.memory_type == "X":
                continue  # an unmapped tag has its own finding
            findings.append(
                ClickFinding(
                    code=CLK_IMMEDIATE_CONTACT_MUST_BE_X,
                    severity=_route_severity(CLK_IMMEDIATE_CONTACT_MUST_BE_X, mode),
                    message=(
                        f"Immediate contact on {tag.name} ({_bank_label(slot)}) at "
                        f"{location_text}; an immediate contact must read an input mapped to X."
                    ),
                    location=location_text,
                    suggestion="Map it to an X input, or use a plain contact.",
                )
            )
        for index, branch in enumerate(rung._branches):
            walk(
                branch,
                ProgramLocation(
                    scope=location.scope,
                    subroutine=location.subroutine,
                    rung_index=location.rung_index,
                    branch_path=(*location.branch_path, index),
                    instruction_index=None,
                    instruction_type=None,
                    arg_path="condition",
                ),
                loops=loops,
            )
        if loops:
            for body_rung in iter_loop_body_rungs(rung._instructions):
                walk(body_rung, location, loops=False)

    def top(scope: Any, subroutine: str | None, rung_index: int) -> ProgramLocation:
        return ProgramLocation(
            scope=scope,
            subroutine=subroutine,
            rung_index=rung_index,
            branch_path=(),
            instruction_index=None,
            instruction_type=None,
            arg_path="condition",
        )

    for rung_index, rung in enumerate(program.rungs):
        walk(rung, top("main", None, rung_index))
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            walk(rung, top("subroutine", sub_name, rung_index))
    return findings


def _is_char_operand(operand: Any) -> bool:
    from pyrung.core.memory_block import IndirectExprRef, IndirectRef
    from pyrung.core.tag import Tag

    if isinstance(operand, Tag):
        return operand.type == TagType.CHAR
    if isinstance(operand, (IndirectRef, IndirectExprRef)):
        return operand.block.type == TagType.CHAR
    return False


def _text_order_compares(condition: Any, out: list[str]) -> None:
    """Collect the operators of ``<``, ``<=``, ``>`` and ``>=`` on a Char operand."""
    from pyrung.core.condition import CompareGe, CompareGt, CompareLe, CompareLt

    symbols = {CompareLt: "<", CompareLe: "<=", CompareGt: ">", CompareGe: ">="}
    if not isinstance(condition, Condition):
        return
    symbol = symbols.get(type(condition))
    if symbol is not None and (
        _is_char_operand(getattr(condition, "tag", None))
        or _is_char_operand(getattr(condition, "value", None))
    ):
        out.append(symbol)
    for attr_name in ("condition", "conditions"):
        attr = getattr(condition, attr_name, None)
        for item in attr if isinstance(attr, (list, tuple)) else [attr]:
            _text_order_compares(item, out)


def _evaluate_text_order_compares(program: Any, mode: ValidationMode) -> list[ClickFinding]:
    """Report each <, <=, > or >= compare on a Char operand.

    CLICK's Compare offers only = and != on text (TXT).
    """
    findings: list[ClickFinding] = []

    def _walk_rung(rung: Any, location_str: str) -> None:
        symbols: list[str] = []
        for cond in rung._conditions:
            _text_order_compares(cond, symbols)
        for symbol in symbols:
            findings.append(
                ClickFinding(
                    code=CLK_TEXT_ORDER_COMPARE,
                    severity=_route_severity(CLK_TEXT_ORDER_COMPARE, mode),
                    message=(
                        f"A text (Char) comparison with '{symbol}' at {location_str}; "
                        "the CLICK Compare dialog offers only = and != on text."
                    ),
                    location=location_str,
                    suggestion=(
                        "Compare text with == or !=, or copy the characters into Int "
                        "tags (convert=to_ascii) and compare those."
                    ),
                )
            )
        for branch in rung._branches:
            _walk_rung(branch, location_str)
        for body_rung in iter_loop_body_rungs(rung._instructions):
            _walk_rung(body_rung, location_str)

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, rung_label(None, rung_index))
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, rung_label(sub_name, rung_index))
    return findings


_HEX_COMPARE_BANKS = frozenset({"XD", "YD", "DH"})


_NUMERIC_COMPARE_BANKS = frozenset({"TD", "CTD", "DS", "DD", "DF", "SD"})


def _compares(program: Any) -> list[tuple[Any, str]]:
    """Return each compare condition of *program* with the location of its rung.

    The walk reaches the conditions of a rung, of its branches, of its instructions and of
    the rungs in a loop body. A condition that several rungs share is there once.
    """
    found: list[tuple[Any, str]] = []
    seen: set[int] = set()

    def _check(condition: Any, location_str: str) -> None:
        if not isinstance(condition, Condition):
            return
        if type(condition).__name__.startswith(("Compare", "IndirectCompare", "ExprCompare")):
            found.append((condition, location_str))
        for attr_name in ("condition", "conditions"):
            attr = getattr(condition, attr_name, None)
            for item in attr if isinstance(attr, (list, tuple)) else [attr]:
                _check(item, location_str)

    def _walk_rung(rung: Any, location_str: str) -> None:
        conditions = list(rung._conditions[rung._branch_condition_start :])
        for instruction in rung._instructions:
            for field_name in getattr(type(instruction), "_conditions", ()):
                value = getattr(instruction, field_name, None)
                conditions.extend(value if isinstance(value, (list, tuple)) else [value])
        for condition in conditions:
            if id(condition) not in seen:
                seen.add(id(condition))
                _check(condition, location_str)
        for branch in rung._branches:
            _walk_rung(branch, location_str)
        for body_rung in iter_loop_body_rungs(rung._instructions):
            _walk_rung(body_rung, location_str)

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, rung_label(None, rung_index))
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, rung_label(sub_name, rung_index))
    return found


def _sides(condition: Any) -> tuple[Any, Any]:
    """Return the left and right operand of a compare condition, or two Nones for an ExprCompare."""
    left = getattr(condition, "tag", None)
    if left is None:
        left = getattr(condition, "indirect_ref", None)
    return left, getattr(condition, "value", None)


def _evaluate_compare_hex_mix(
    program: Any, mode: ValidationMode, resolver: _ResolutionCache
) -> list[ClickFinding]:
    """Report a compare that pairs a hex operand (XD, YD, DH) with a decimal one.

    CLICK's editor refuses a hex operand paired with a decimal one. A pasted rung skips
    the check.
    """
    from pyrung.core.validation.render import render_condition

    findings: list[ClickFinding] = []

    def _group(operand: Any) -> str | None:
        if not isinstance(operand, (Tag, IndirectRef, IndirectExprRef)):
            return None
        resolution = resolver.resolve_operand_slots(operand)
        banks = {slot.memory_type for slot in resolution.slots}
        if banks and banks <= _HEX_COMPARE_BANKS:
            return "hex"
        if banks and banks <= _NUMERIC_COMPARE_BANKS:
            return "numeric"
        return None

    for condition, location_str in _compares(program):
        left, right = _sides(condition)
        if {_group(left), _group(right)} == {"hex", "numeric"}:
            findings.append(
                ClickFinding(
                    code=CLK_COMPARE_HEX_MIX,
                    severity=_route_severity(CLK_COMPARE_HEX_MIX, mode),
                    message=(
                        f"{render_condition(condition)} at {location_str} compares a hex "
                        "operand (XD, YD, DH) with a decimal one (DS, DD, DF, TD, CTD, SD). "
                        "CLICK's Compare dialog pairs a hex operand only with another "
                        "hex operand or an h constant."
                    ),
                    location=location_str,
                    suggestion=(
                        "Copy the Word into a DS tag first and compare that, or compare "
                        "it with a DH tag or a constant."
                    ),
                )
            )
    return findings


def _evaluate_compare_text_mix(
    program: Any, mode: ValidationMode, resolver: _ResolutionCache
) -> list[ClickFinding]:
    """Report a compare that pairs text with a number, or a TXT register with a number register.

    CLICK's Compare refuses it (OBJ-007). The finding has laddercodec's code for the
    refusal, so ``allow=["compare-mixed-types"]`` lets it through.
    """
    from laddercodec import Code

    from pyrung.core.validation.render import render_condition

    findings: list[ClickFinding] = []

    def _is_text(operand: Any) -> bool | None:
        # None for an operand that holds no data bank, such as an expression.
        if isinstance(operand, str):
            return True
        if isinstance(operand, (int, float)) and not isinstance(operand, bool):
            return False
        if not isinstance(operand, (Tag, IndirectRef, IndirectExprRef)):
            return None
        banks = {slot.memory_type for slot in resolver.resolve_operand_slots(operand).slots}
        if banks == {"TXT"}:
            return True
        if banks and banks <= _HEX_COMPARE_BANKS | _NUMERIC_COMPARE_BANKS:
            return False
        return None

    for condition, location_str in _compares(program):
        left, right = _sides(condition)
        if {_is_text(left), _is_text(right)} == {True, False}:
            findings.append(
                ClickFinding(
                    code=Code.COMPARE_MIXED_TYPES.value,
                    severity=_route_severity(Code.COMPARE_MIXED_TYPES.value, mode),
                    message=(
                        f"{render_condition(condition)} at {location_str} compares text with "
                        "a number. CLICK's Compare takes a TXT register or a text constant "
                        "on both sides, or a number on both sides (OBJ-007)."
                    ),
                    location=location_str,
                    suggestion=(
                        "Compare text with text. To compare a character's code, copy the "
                        "character into an Int tag first (convert=to_ascii)."
                    ),
                )
            )
    return findings


def _evaluate_text_pair_compares(program: Any, mode: ValidationMode) -> list[ClickFinding]:
    """Report a compare of two text constants that the CSV reads as two different compares.

    Only an inline expression has a text on both sides, such as
    ``LiteralExpr("A") == LiteralExpr("B")``. The export refuses the compare even when the
    finding is allowed, so warn mode reports it as a warning.
    """
    from laddercodec import Code

    from pyrung.click.ladder.translator import (
        TEXT_PAIR_ADVICE,
        text_constant_token,
        text_pair_reads_two_ways,
    )
    from pyrung.core.expression import ExprCompare, LiteralExpr
    from pyrung.core.validation.render import render_condition

    findings: list[ClickFinding] = []
    for condition, location_str in _compares(program):
        if not isinstance(condition, ExprCompare):
            continue
        left, right = condition.left, condition.right
        if not all(
            isinstance(side, LiteralExpr) and isinstance(side.value, str) for side in (left, right)
        ):
            continue
        token = (
            f"{text_constant_token(left.value)}{condition.symbol}{text_constant_token(right.value)}"
        )
        if not text_pair_reads_two_ways(token):
            continue
        code = Code.INVALID_TEXT_CONSTANT.value
        findings.append(
            ClickFinding(
                code=code,
                severity="error" if mode == "strict" else "warning",
                message=(
                    f"{render_condition(condition)} at {location_str} compares two texts, and "
                    "the CSV reads it as two different compares."
                ),
                location=location_str,
                suggestion=TEXT_PAIR_ADVICE,
            )
        )
    return findings


CHECKS = (
    Check(
        "program",
        20,
        lambda context: _evaluate_immediate_contacts(
            context.program, context.tag_map, context.mode, context.resolver
        ),
    ),
    Check(
        "program", 40, lambda context: _evaluate_text_order_compares(context.program, context.mode)
    ),
    Check(
        "program",
        50,
        lambda context: _evaluate_compare_hex_mix(context.program, context.mode, context.resolver),
    ),
    Check(
        "program",
        55,
        lambda context: _evaluate_compare_text_mix(context.program, context.mode, context.resolver),
    ),
    Check(
        "program", 56, lambda context: _evaluate_text_pair_compares(context.program, context.mode)
    ),
)

TABLES: dict[str, dict[Any, Any]] = {}
