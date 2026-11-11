"""Structural portability checks for Click validation."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pyrung.core.expression import (
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
)
from pyrung.core.instruction.calc import CalcMode, infer_calc_mode
from pyrung.core.instruction.control import (
    CallInstruction,
    ForLoopInstruction,
    ReturnInstruction,
    iter_loop_body_rungs,
)
from pyrung.core.memory_block import (
    BlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation.walker import ProgramLocation

from .findings import (
    CLK_EXPR_ONLY_IN_MATH,
    CLK_FORLOOP_COUNT_BANK,
    CLK_FORLOOP_INNER_ONESHOT,
    CLK_FORLOOP_NESTING_DEPTH,
    CLK_FORLOOP_RETURN_INSIDE,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTACT_MUST_BE_X,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_MATH_ARG_NOT_PLAIN,
    CLK_MATH_BIT_OPERAND,
    CLK_MATH_BIT_RANGE_AGGREGATE,
    CLK_MATH_FLOOR_DIV,
    CLK_MATH_FUNC_MODE_MISMATCH,
    CLK_MATH_MEDIAN_RANGE_TOO_LARGE,
    CLK_MATH_MODE_MIXED,
    CLK_MATH_NESTING_DEPTH,
    CLK_MATH_RANGE_MIXED_BANKS,
    CLK_MATH_RANGE_NOT_CONTIGUOUS,
    CLK_MATH_SUM_BANK_NOT_AVAILABLE,
    CLK_MATH_WIDE_CONSTANT,
    CLK_NESTED_CALL,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_RAND_BOUND_TYPE,
    CLK_RAND_EQUAL_BOUNDS,
    CLK_STATUS_BIT_NOT_PORTABLE,
    CLK_TEXT_ORDER_COMPARE,
    CLK_TEXT_WINDOW_NOT_CONTIGUOUS,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    ClickFinding,
    ValidationMode,
    _build_suggestion,
    _route_severity,
)
from .resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _ResolutionCache,
    _ResolvedSlot,
    _unique_slots,
    _unresolved_finding,
)

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import OperandFact


def _evaluate_fact(
    fact: OperandFact,
    tag_map: TagMap,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Apply Stage 2 rules to a single OperandFact."""
    findings: list[ClickFinding] = []
    loc = fact.location
    location_str = _format_location(loc)

    if fact.value_kind == "indirect_ref":
        allowed = loc.instruction_type == "CopyInstruction" and loc.arg_path in {
            "instruction.source",
            "instruction.source.source",
            "instruction.dest",
            "instruction.target",
        }
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_PTR_CONTEXT_ONLY_COPY,
                    severity=_route_severity(CLK_PTR_CONTEXT_ONLY_COPY, mode),
                    message=f"Pointer (IndirectRef) used outside copy instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_PTR_CONTEXT_ONLY_COPY, fact, tag_map),
                )
            )
        else:
            pointer_name = str(fact.metadata.get("pointer_name", ""))
            pointer_block_name = fact.metadata.get("pointer_block_name")
            memory_type = (
                resolver.resolve_block_memory_type(pointer_block_name)
                if isinstance(pointer_block_name, str)
                else None
            )
            if memory_type is None:
                memory_type = resolver.resolve_pointer_memory_type(pointer_name)
            if memory_type is None:
                code = CLK_PTR_DS_UNVERIFIED
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"Pointer '{pointer_name}' memory type could not be verified "
                            f"as DS at {location_str}."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )
            elif memory_type != "DS":
                code = CLK_PTR_POINTER_MUST_BE_DS
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"Pointer '{pointer_name}' is mapped to {memory_type}, "
                            f"not DS at {location_str}."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )

    if fact.value_kind == "indirect_expr_ref":
        findings.append(
            ClickFinding(
                code=CLK_PTR_EXPR_NOT_ALLOWED,
                severity=_route_severity(CLK_PTR_EXPR_NOT_ALLOWED, mode),
                message=f"Computed pointer expression (IndirectExprRef) not allowed at {location_str}.",
                location=location_str,
                suggestion=_build_suggestion(CLK_PTR_EXPR_NOT_ALLOWED, fact, tag_map),
            )
        )

    if fact.value_kind == "expression":
        allowed = (
            loc.instruction_type == "CalcInstruction" and loc.arg_path == "instruction.expression"
        )
        if not allowed:
            findings.append(
                ClickFinding(
                    code=CLK_EXPR_ONLY_IN_MATH,
                    severity=_route_severity(CLK_EXPR_ONLY_IN_MATH, mode),
                    message=f"Expression used outside a math() instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_EXPR_ONLY_IN_MATH, fact, tag_map),
                )
            )
        expr_dsl = str(fact.metadata.get("expr_dsl", ""))
        if "~" in expr_dsl:
            findings.append(
                ClickFinding(
                    code=CLK_TILDE_BOOL_CONTACT_ONLY,
                    severity=_route_severity(CLK_TILDE_BOOL_CONTACT_ONLY, mode),
                    message=(
                        f"Expression uses `~` (bitwise invert) at {location_str}. "
                        "Click portability reserves `~` for BOOL contact inversion."
                    ),
                    location=location_str,
                    suggestion=_build_suggestion(CLK_TILDE_BOOL_CONTACT_ONLY, fact, tag_map),
                )
            )

    if (
        fact.value_kind == "condition"
        and fact.metadata.get("condition_type") == "IntTruthyCondition"
    ):
        findings.append(
            ClickFinding(
                code=CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
                severity=_route_severity(CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED, mode),
                message=f"Implicit numeric truthiness used in condition at {location_str}.",
                location=location_str,
                suggestion=_build_suggestion(
                    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED, fact, tag_map
                ),
            )
        )

    if fact.value_kind == "indirect_block_range":
        findings.append(
            ClickFinding(
                code=CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
                severity=_route_severity(CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED, mode),
                message=(
                    f"IndirectBlockRange not allowed at {location_str}. "
                    "Click hardware does not support computed block ranges."
                ),
                location=location_str,
                suggestion=_build_suggestion(CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED, fact, tag_map),
            )
        )

    return findings


def _location_key(
    location: ProgramLocation,
) -> tuple[str, str | None, int, tuple[int, ...], int | None, str | None]:
    return (
        location.scope,
        location.subroutine,
        location.rung_index,
        location.branch_path,
        location.instruction_index,
        location.instruction_type,
    )


def _evaluate_immediate_coil_target(
    immediate_ref: ImmediateRef,
    location: ProgramLocation,
    tag_map: TagMap,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    wrapped = immediate_ref.value
    findings: list[ClickFinding] = []
    location_text = _format_location(location)
    slots: list[_ResolvedSlot] = []

    if isinstance(wrapped, Tag):
        resolved = resolver.resolve_direct_tag(wrapped)
        if resolved is None:
            return [
                _unresolved_finding(
                    location,
                    mode,
                    "immediate coil target mapping missing or ambiguous",
                )
            ]
        slots = [resolved]
    elif isinstance(wrapped, BlockRange):
        for tag in wrapped.tags():
            resolved = resolver.resolve_direct_tag(tag)
            if resolved is None:
                return [
                    _unresolved_finding(
                        location,
                        mode,
                        "immediate coil range mapping missing or ambiguous",
                    )
                ]
            slots.append(resolved)
    else:
        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                message=(
                    f"Immediate wrapper must wrap Tag or BlockRange at {location_text}, "
                    f"got {type(wrapped).__name__}."
                ),
                location=location_text,
                suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, None, tag_map),
            )
        )
        return findings

    non_y_slots = [slot for slot in slots if slot.memory_type != "Y"]
    if non_y_slots:
        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
                severity=_route_severity(CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y, mode),
                message=(
                    f"Immediate coil target must resolve to Y bank at {location_text}, "
                    f"found {', '.join(_bank_label(slot) for slot in _unique_slots(non_y_slots))}."
                ),
                location=location_text,
                suggestion=_build_suggestion(CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y, None, tag_map),
            )
        )

    if isinstance(wrapped, BlockRange) and len(slots) > 1:
        addresses = [slot.address for slot in slots]
        if any(address is None for address in addresses):
            findings.append(
                _unresolved_finding(location, mode, "immediate range address unresolved")
            )
        else:
            numeric_addresses = [int(address) for address in addresses if address is not None]
            contiguous = all(
                numeric_addresses[idx] + 1 == numeric_addresses[idx + 1]
                for idx in range(len(numeric_addresses) - 1)
            )
            if not contiguous:
                findings.append(
                    ClickFinding(
                        code=CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
                        severity=_route_severity(CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS, mode),
                        message=(
                            "Immediate coil range must map to contiguous addresses "
                            f"at {location_text}."
                        ),
                        location=location_text,
                        suggestion=_build_suggestion(
                            CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS, None, tag_map
                        ),
                    )
                )

    return findings


def _evaluate_immediate_contacts(
    program: Any, tag_map: TagMap, mode: ValidationMode, resolver: _ResolutionCache
) -> list[ClickFinding]:
    """An immediate NO/NC contact must read an input mapped to X: CLICK offers it on X only."""
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
        # iter_loop_body_rungs yields nested loops' rungs too: walk those without it.
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
                    suggestion=_build_suggestion(CLK_IMMEDIATE_CONTACT_MUST_BE_X, None, tag_map),
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


def _evaluate_immediate_usage(
    facts: tuple[OperandFact, ...],
    instruction_sites: list[tuple[Any, ProgramLocation]],
    tag_map: TagMap,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    condition_types: dict[
        tuple[tuple[str, str | None, int, tuple[int, ...], int | None, str | None], str], str
    ] = {}
    instructions_by_site: dict[
        tuple[str, str | None, int, tuple[int, ...], int | None, str | None], Any
    ] = {}

    for fact in facts:
        if fact.value_kind != "condition":
            continue
        condition_type = fact.metadata.get("condition_type")
        if not isinstance(condition_type, str):
            continue
        condition_types[(_location_key(fact.location), fact.location.arg_path)] = condition_type

    for instruction, location in instruction_sites:
        instructions_by_site[_location_key(location)] = instruction

    for fact in facts:
        if fact.value_kind != "immediate_ref":
            continue

        loc = fact.location
        location_text = _format_location(loc)
        site_key = _location_key(loc)

        if loc.instruction_index is None:
            parent_path = loc.arg_path.rsplit(".", 1)[0] if "." in loc.arg_path else ""
            condition_type = condition_types.get((site_key, parent_path))

            if condition_type in {"BitCondition", "NormallyClosedCondition"}:
                continue
            if condition_type in {"RisingEdgeCondition", "FallingEdgeCondition"}:
                findings.append(
                    ClickFinding(
                        code=CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
                        severity=_route_severity(CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED, mode),
                        message=f"Immediate edge contact is not allowed at {location_text}.",
                        location=location_text,
                        suggestion=_build_suggestion(
                            CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED, fact, tag_map
                        ),
                    )
                )
                continue

            findings.append(
                ClickFinding(
                    code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                    severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                    message=f"Immediate wrapper is not allowed at {location_text}.",
                    location=location_text,
                    suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, fact, tag_map),
                )
            )
            continue

        if (
            loc.instruction_type
            in {"OutInstruction", "LatchInstruction", "ResetInstruction", "ToggleInstruction"}
            and loc.arg_path == "instruction.target"
        ):
            instruction = instructions_by_site.get(site_key)
            target = getattr(instruction, "target", None)
            if isinstance(target, ImmediateRef):
                findings.extend(
                    _evaluate_immediate_coil_target(target, loc, tag_map, mode, resolver)
                )
            continue

        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                message=f"Immediate wrapper is not allowed at {location_text}.",
                location=location_text,
                suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, fact, tag_map),
            )
        )

    return findings


def _expr_contains_floor_div(expr: Any) -> bool:
    """Return True if the expression tree contains a floor-division node."""
    if isinstance(expr, BinaryExpr) and expr.symbol == "//":
        return True
    if isinstance(expr, Expression):
        return any(_expr_contains_floor_div(v) for v in vars(expr).values())
    return False


# ---------------------------------------------------------------------------
# Click formula-pad parenthesization depth
# ---------------------------------------------------------------------------
# The translator wraps binary sub-expressions of binary/unary-prefix nodes in
# explicit parens, and function calls (SQRT, LSH, abs, …) each add one paren
# level.  This mirrors the rendered Click formula string.

# Maximum parenthetical nesting the Click formula pad accepts.
_CLICK_MAX_PAREN_DEPTH = 8


def _is_negative_operand(expr: Any) -> bool:
    """A right operand the translator parenthesizes for its sign.

    ``DS1 + ( -5 )`` for a literal, ``DS1 * ( 0 - DS2 )`` for a negation.

    Counts a negative literal even in hex mode, where it renders unsigned
    (``FFFFh``) and gets no parens; that only overstates the depth.
    """
    if isinstance(expr, UnaryExpr):
        return expr.symbol == "-"
    return (
        isinstance(expr, LiteralExpr)
        and isinstance(expr.value, (int, float))
        and not isinstance(expr.value, bool)
        and expr.value < 0
    )


def _expr_paren_depth(expr: Any) -> int:
    """Return the max parenthesization depth the Click formula would have."""
    if not isinstance(expr, Expression):
        return 0

    # Leaves
    if isinstance(expr, (TagExpr, LiteralExpr)):
        return 0

    # Range aggregates: SUM ( a : b ), MAX ( a : b ), ... — one paren level
    if isinstance(expr, (SumExpr, RangeAggregateExpr)):
        return 1

    # RAND ( lo , hi ) — one paren level
    if isinstance(expr, RandIntExpr):
        return max(_expr_paren_depth(expr.lo), _expr_paren_depth(expr.hi)) + 1

    # ShiftFuncExpr: FUNC(val, cnt) — one paren level
    if isinstance(expr, ShiftFuncExpr):
        return max(_expr_paren_depth(expr.value), _expr_paren_depth(expr.count)) + 1

    # MathFuncExpr: FUNC(operand) — one paren level
    if isinstance(expr, MathFuncExpr):
        return _expr_paren_depth(expr.operand) + 1

    # Unary expressions
    if isinstance(expr, UnaryExpr):
        child_depth = _expr_paren_depth(expr.operand)
        # abs is a function call — always adds a paren level
        if expr.symbol == "abs":
            return child_depth + 1
        # Prefix unary: parens added only when operand is binary
        if isinstance(expr.operand, BinaryExpr):
            return child_depth + 1
        return child_depth

    # Binary operators (including << >> which render as Click shift functions)
    if isinstance(expr, BinaryExpr):
        # << and >> render as LSH/RSH function calls — one paren level
        if expr.symbol in ("<<", ">>"):
            return max(_expr_paren_depth(expr.left), _expr_paren_depth(expr.right)) + 1
        left_depth = _expr_paren_depth(expr.left)
        if isinstance(expr.left, BinaryExpr) or (
            isinstance(expr.left, UnaryExpr) and expr.left.symbol == "-"
        ):
            left_depth += 1
        right_depth = _expr_paren_depth(expr.right)
        if isinstance(expr.right, BinaryExpr) or _is_negative_operand(expr.right):
            right_depth += 1
        return max(left_depth, right_depth)

    return 0


# Click formula-pad mode restrictions:
#   decimal: +, -, *, /, MOD, ^, SUM, MAX, MIN, MDN, AVG, ABS,
#            SIN, ASIN, COS, ACOS, TAN, ATAN, SQRT, LOG, LN, RAD, DEG, PI
#   hex:     +, -, *, /, MOD, SUM, MAX, MIN, MDN, AVG, AND, OR, XOR, LSH, RSH, LRO, RRO
# ABS is a UnaryExpr (``abs(x)``), not a MathFuncExpr, so it has its own arm below.

_DECIMAL_ONLY_SYMBOLS = frozenset({"**"})
_DECIMAL_ONLY_MATH_FUNCS = frozenset(
    {"sqrt", "sin", "cos", "tan", "asin", "acos", "atan", "radians", "degrees", "log10", "log"}
)

_HEX_ONLY_SYMBOLS = frozenset({"&", "|", "^", "<<", ">>"})
_HEX_ONLY_SHIFT_FUNCS = frozenset({"lsh", "rsh", "lro", "rro"})

# Click name for human-readable messages
_CLICK_OP_NAME: dict[str, str] = {
    "**": "^ (power)",
    "&": "AND",
    "|": "OR",
    "^": "XOR",
    "<<": "LSH",
    ">>": "RSH",
}


def _collect_mode_violations(expr: Any, calc_mode: CalcMode) -> list[str]:
    """Return Click-native names of operators/functions that violate the calc mode."""
    violations: list[str] = []
    _walk_mode_violations(expr, calc_mode, violations, set())
    return violations


def _walk_mode_violations(
    expr: Any, calc_mode: CalcMode, violations: list[str], seen: set[int]
) -> None:
    expr_id = id(expr)
    if expr_id in seen:
        return
    seen.add(expr_id)

    if not isinstance(expr, Expression):
        return

    if isinstance(expr, BinaryExpr):
        if calc_mode == "decimal" and expr.symbol in _HEX_ONLY_SYMBOLS:
            violations.append(_CLICK_OP_NAME.get(expr.symbol, expr.symbol))
        elif calc_mode == "hex" and expr.symbol in _DECIMAL_ONLY_SYMBOLS:
            violations.append(_CLICK_OP_NAME.get(expr.symbol, expr.symbol))
    if isinstance(expr, ShiftFuncExpr) and expr.name in _HEX_ONLY_SHIFT_FUNCS:
        if calc_mode == "decimal":
            violations.append(expr.name.upper())
    if isinstance(expr, MathFuncExpr) and expr.name in _DECIMAL_ONLY_MATH_FUNCS:
        if calc_mode == "hex":
            from pyrung.click.ladder.translator import _MATH_FUNC_CLICK_NAME

            violations.append(_MATH_FUNC_CLICK_NAME.get(expr.name, expr.name.upper()))
    if isinstance(expr, UnaryExpr) and expr.symbol == "abs" and calc_mode == "hex":
        violations.append("ABS")

    for child in vars(expr).values():
        _walk_mode_violations(child, calc_mode, violations, seen)


# ---------------------------------------------------------------------------
# Range aggregates: SUM / MAX / MIN / MDN / AVG ( first : last )
# ---------------------------------------------------------------------------
# CLICK's object error table: the range must be ordered Low:High (OBJ-1014),
# both ends in the same address block (OBJ-1015), and an MDN range is limited
# to 1024 bytes (OBJ-1013).  The exporter writes ``FUNC ( first : last )`` from
# the range's first and last mapped tag, so every member in between must map
# to the addresses CLICK will read.

_BIT_BANKS = frozenset({"X", "Y", "C", "T", "CT", "SC"})
# SUM reads X, Y, C, T, SC, DS, DD, DF, TD, CTD, SD in decimal mode and DH, XD, YD in hex
# mode.  Only these two banks are out in both.  A bank in the wrong mode needs no check here:
# TagMap ties a tag's type to its bank, and a mixed math() gets CLK_MATH_MODE_MIXED.
_NO_SUM_BANKS = frozenset({"CT", "TXT"})
# Banks whose CLICK address space has slot gaps (X001..X016, X101..X116, ...).
_SLOTTED_BANKS = frozenset({"X", "Y"})
_MDN_MAX_BYTES = 1024
_BANK_BYTES: dict[str, int] = {"DD": 4, "DF": 4, "CTD": 4}


def _collect_range_aggregates(expr: Any, out: list[tuple[str, BlockRange]], seen: set[int]) -> None:
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, SumExpr):
        out.append(("sum", expr.block_range))
        return
    if isinstance(expr, RangeAggregateExpr):
        out.append((expr.kind, expr.block_range))
        return
    for child in vars(expr).values():
        _collect_range_aggregates(child, out, seen)


def _evaluate_range_aggregates(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Check each math() range aggregate against CLICK's range rules."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    from pyrung.click.ladder.translator import _RANGE_AGGREGATE_CLICK_NAME

    aggregates: list[tuple[str, BlockRange]] = []
    _collect_range_aggregates(instruction.source_expression, aggregates, set())
    if not aggregates:
        return []

    location_text = _format_location(_instruction_location(base_location, "instruction.expression"))
    findings: list[ClickFinding] = []

    def emit(code: str, message: str, suggestion: str) -> None:
        findings.append(
            ClickFinding(
                code=code,
                severity=_route_severity(code, mode),
                message=f"{message} at {location_text}.",
                location=location_text,
                suggestion=suggestion,
            )
        )

    for kind, block_range in aggregates:
        click_name = _RANGE_AGGREGATE_CLICK_NAME[kind]
        slots = [resolver.resolve_direct_tag(tag) for tag in block_range.tags()]
        if not slots or any(slot is None for slot in slots):
            continue  # unresolved mappings are reported by the bank checks
        resolved = [slot for slot in slots if slot is not None]
        banks = list(dict.fromkeys(slot.memory_type for slot in resolved))
        if len(banks) > 1:
            emit(
                CLK_MATH_RANGE_MIXED_BANKS,
                f"{click_name} range spans more than one address block ({', '.join(banks)})",
                f"Map every member of the {click_name} range into one bank, or split the "
                "math() into one aggregate per bank.",
            )
            continue
        bank = banks[0]
        if kind == "sum":
            if bank in _NO_SUM_BANKS:
                emit(
                    CLK_MATH_SUM_BANK_NOT_AVAILABLE,
                    f"SUM over {bank} is not available in CLICK",
                    "Copy the values into a bank SUM accepts before summing.",
                )
        elif bank in _BIT_BANKS:
            emit(
                CLK_MATH_BIT_RANGE_AGGREGATE,
                f"{click_name} over bit bank {bank} is not available in CLICK; "
                "only SUM accepts a bit range",
                f"Use .sum() to count the True bits, or copy the bits into a register "
                f"bank before taking {click_name}.",
            )
        addresses = [slot.address for slot in resolved]
        if any(address is None for address in addresses):
            continue
        numbers = [int(address) for address in addresses if address is not None]
        if bank in _SLOTTED_BANKS:
            ordered = all(a < b for a, b in zip(numbers, numbers[1:], strict=False))
        else:
            ordered = all(a + 1 == b for a, b in zip(numbers, numbers[1:], strict=False))
        if not ordered:
            emit(
                CLK_MATH_RANGE_NOT_CONTIGUOUS,
                f"{click_name} range does not map to one contiguous low:high run of {bank} "
                f"addresses (first {bank}{numbers[0]}, last {bank}{numbers[-1]})",
                "Map the range to consecutive addresses in ascending order, and drop "
                ".reverse(): the aggregate does not depend on member order.",
            )
        if kind == "median":
            size = len(numbers) * _BANK_BYTES.get(bank, 2)
            if size > _MDN_MAX_BYTES:
                emit(
                    CLK_MATH_MEDIAN_RANGE_TOO_LARGE,
                    f"MDN range is {size} bytes; CLICK allows at most {_MDN_MAX_BYTES} "
                    "(512 DS/DH or 256 DD/DF registers)",
                    "Shrink the median range, or take the median of smaller ranges in "
                    "separate math() steps.",
                )
    return findings


def _collect_bit_operands(expr: Any, out: list[Tag], seen: set[int]) -> None:
    """Each Bool tag *expr* reads as an operand, outside a range aggregate (whose
    rules, SUM over bits included, are :func:`_evaluate_range_aggregates`') and a
    RAND (a bit bound is ``CLK_RAND_BOUND_TYPE``)."""
    if isinstance(expr, ImmediateRef):
        expr = expr.value
    if isinstance(expr, Tag):
        if expr.type is TagType.BOOL:
            out.append(expr)
        return
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, TagExpr):
        _collect_bit_operands(expr.tag, out, seen)
        return
    if isinstance(expr, (SumExpr, RangeAggregateExpr, RandIntExpr)):
        return
    for child in vars(expr).values():
        _collect_bit_operands(child, out, seen)


def _evaluate_math_bit_operands(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """CLK_MATH_BIT_OPERAND: CLICK's Math editor refuses a bit in its formula, except
    in a SUM over a bit range (checked in the v4 editor, 2026-11-06)."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    bits: list[Tag] = []
    _collect_bit_operands(instruction.source_expression, bits, set())
    if not bits:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.expression"))
    names = list(dict.fromkeys(tag.name for tag in bits))
    what = "the bit" if len(names) == 1 else "the bits"
    # CLICK has no SUM over CT, so a one-bit range cannot stand in for a CT bit.
    counter_bits = all(re.fullmatch(r"CT\d+", name) for name in names)
    return [
        ClickFinding(
            code=CLK_MATH_BIT_OPERAND,
            severity=_route_severity(CLK_MATH_BIT_OPERAND, mode),
            message=(
                f"math() at {location_text} reads {what} {', '.join(names)}; CLICK's Math "
                "takes no bit in its formula, except in a SUM over a bit range."
            ),
            location=location_text,
            suggestion=(
                ""
                if counter_bits
                else "Write the bit as a one-bit range, c.select(1, 1).sum() for C1 "
                "(SUM ( C1 : C1 )), or "
            )
            + "set a DS tag from the bit's contacts in a rung above (copy(1, Tag) under "
            "the bit, copy(0, Tag) under ~bit) and use the tag."
            + (
                ""
                if counter_bits
                else " A one-bit range of pyrung's sc block reads 0 in pyrung, "
                "where a CLICK reads the bit."
            ),
        )
    ]


def _is_status_bit_tag(tag: Any) -> bool:
    """Return True if tag belongs to a final field on a DoneAcc timer/counter structure."""
    from pyrung.core.structure import _DoneAccRuntime

    block = getattr(tag, "_pyrung_block", None)
    if block is None:
        return False
    if not getattr(block, "_pyrung_field_final", False):
        return False
    runtime = getattr(block, "_pyrung_structure_runtime", None)
    return isinstance(runtime, _DoneAccRuntime)


def _collect_condition_tags(condition: Any) -> list[Any]:
    """Recursively collect all Tag objects referenced by a condition tree."""
    from pyrung.core.condition import Condition
    from pyrung.core.tag import Tag

    tags: list[Any] = []
    if isinstance(condition, Tag):
        tags.append(condition)
    elif isinstance(condition, Condition):
        for attr_name in ("tag", "target", "left", "right", "condition", "conditions"):
            attr = getattr(condition, attr_name, None)
            if attr is None:
                continue
            if isinstance(attr, Tag):
                tags.append(attr)
            elif isinstance(attr, (list, tuple)):
                for item in attr:
                    tags.extend(_collect_condition_tags(item))
            elif isinstance(attr, Condition):
                tags.extend(_collect_condition_tags(attr))
    return tags


def _evaluate_status_bit_usage(
    program: Any,
    mode: ValidationMode,
) -> list[ClickFinding]:
    """Flag any reference to timer/counter status bits (EN, TT, CU, CD) in conditions."""
    findings: list[ClickFinding] = []
    seen: set[str] = set()

    def _check_tag(tag: Any, location_str: str) -> None:
        if not _is_status_bit_tag(tag):
            return
        name = tag.name
        if name in seen:
            return
        seen.add(name)
        findings.append(
            ClickFinding(
                code=CLK_STATUS_BIT_NOT_PORTABLE,
                severity=_route_severity(CLK_STATUS_BIT_NOT_PORTABLE, mode),
                message=(
                    f"Tag '{name}' is a simulation-only status bit at {location_str}. "
                    "Click hardware manages timer/counter status bits internally."
                ),
                location=location_str,
                suggestion=(
                    "Use the corresponding Done/Acc fields instead, or remove this "
                    "reference from Click-targeted logic. Status bits (EN, TT, CU, CD) "
                    "are available in the pyrung runner API (.when(), .monitor()) but "
                    "cannot appear in Click ladder."
                ),
            )
        )

    def _walk_rung(rung: Any, location_str: str) -> None:
        for cond in rung._conditions:
            for tag in _collect_condition_tags(cond):
                _check_tag(tag, location_str)
        for branch in rung._branches:
            _walk_rung(branch, location_str)
        for body_rung in iter_loop_body_rungs(rung._instructions):
            _walk_rung(body_rung, location_str)

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, f"main rung {rung_index}")

    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, f"subroutine '{sub_name}' rung {rung_index}")

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
    from pyrung.core.condition import CompareGe, CompareGt, CompareLe, CompareLt, Condition

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
    """CLICK's Compare offers only = and != on text (TXT)."""
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
        _walk_rung(rung, f"main rung {rung_index}")
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, f"subroutine '{sub_name}' rung {rung_index}")
    return findings


def _text_literal_compares(condition: Any, out: list[tuple[Any, list[Tag]]]) -> None:
    """Collect each ``==``/``!=`` of a Char with a longer literal, and the Chars pyrung reads.

    A text window (``txt[1] == "AB"``) reads the Chars that follow in its block
    (``text_window_condition``); any other such compare reads its one Char
    against the whole literal.
    """
    from pyrung.core.condition import CompareEq, CompareNe, Condition, text_window_of

    if isinstance(condition, (list, tuple)):
        for item in condition:
            _text_literal_compares(item, out)
        return
    if not isinstance(condition, Condition):
        return
    written = text_window_of(condition)
    if written is not condition:
        out.append((written, [child.tag for child in getattr(condition, "conditions", ())]))
        return
    if (
        isinstance(condition, (CompareEq, CompareNe))
        and condition.tag.type == TagType.CHAR
        and isinstance(condition.value, str)
        and len(condition.value) > 1
    ):
        out.append((condition, [condition.tag]))
        return
    for attr_name in ("condition", "conditions"):
        _text_literal_compares(getattr(condition, attr_name, None), out)


#: CLICK's last TXT address.
_TXT_LAST = 1000


def _evaluate_text_windows(
    program: Any, mode: ValidationMode, resolver: _ResolutionCache
) -> list[ClickFinding]:
    """A CLICK compares a longer literal over a TXT and the TXTs after it.

    pyrung compares the Chars that follow in the Char's block, so the two agree
    only where those are mapped to the next TXT addresses, one for each character.
    """
    from pyrung.core.validation.render import render_condition

    findings: list[ClickFinding] = []

    def _check(written: Any, tags: list[Tag], location_str: str) -> None:
        slots = [resolver.resolve_direct_tag(tag) for tag in tags]
        resolved = [slot for slot in slots if slot is not None and slot.address is not None]
        if len(resolved) != len(tags):
            return  # unresolved mappings are reported by the bank checks
        first = resolved[0]
        if first.memory_type != "TXT" or first.address is None:
            return
        expected = [f"TXT{first.address + offset}" for offset in range(len(written.value))]
        actual = [_bank_label(slot) for slot in resolved]
        if actual == expected:
            return
        last = first.address + len(written.value) - 1
        if last > _TXT_LAST:
            click_reads = (
                f"a CLICK compares TXT{first.address} and the TXTs after it, "
                f"which run past TXT{_TXT_LAST}"
            )
        else:
            click_reads = f"a CLICK compares {', '.join(expected)}, one character each"
        if len(tags) == 1:
            read = f"compares {tags[0].name} ({actual[0]}) alone with the whole literal"
        else:
            pairs = ", ".join(
                f"{tag.name} ({label})" for tag, label in zip(tags, actual, strict=True)
            )
            read = f"compares {pairs}"
        findings.append(
            ClickFinding(
                code=CLK_TEXT_WINDOW_NOT_CONTIGUOUS,
                severity=_route_severity(CLK_TEXT_WINDOW_NOT_CONTIGUOUS, mode),
                message=(
                    f"{render_condition(written)} at {location_str} {read} in pyrung; "
                    f"{click_reads}."
                ),
                location=location_str,
                suggestion=(
                    "Compare one character at a time, or map the Chars to consecutive "
                    "TXT addresses in order (a Char block mapped to a txt.select() range)."
                ),
            )
        )

    # A timer's or counter's enable (and a drum's, an enabled call's) holds the
    # rung's own conditions again, so each compare is checked once, where first met.
    seen: set[int] = set()

    def _walk_rung(rung: Any, location_str: str) -> None:
        compares: list[tuple[Any, list[Tag]]] = []
        _text_literal_compares(rung._conditions[rung._branch_condition_start :], compares)
        for instruction in rung._instructions:
            for field_name in getattr(type(instruction), "_conditions", ()):
                _text_literal_compares(getattr(instruction, field_name, None), compares)
        for written, tags in compares:
            if id(written) in seen:
                continue
            seen.add(id(written))
            _check(written, tags, location_str)
        for branch in rung._branches:
            _walk_rung(branch, location_str)
        for body_rung in iter_loop_body_rungs(rung._instructions):
            _walk_rung(body_rung, location_str)

    for rung_index, rung in enumerate(program.rungs):
        _walk_rung(rung, f"main rung {rung_index}")
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, f"subroutine '{sub_name}' rung {rung_index}")
    return findings


def _evaluate_instruction_portability(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    if instruction_type == "CalcInstruction":
        mode_info = infer_calc_mode(instruction.source_expression, instruction.dest)
        if mode_info.mixed_families:
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_MATH_MODE_MIXED,
                    severity=_route_severity(CLK_MATH_MODE_MIXED, mode),
                    message=(
                        "math() mixes WORD (hex-family) and non-WORD (decimal-family) operands "
                        f"at {location_text}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Split mixed math into separate decimal and WORD-only math() steps, "
                        "or convert through an intermediate tag so each math() stays one family."
                    ),
                )
            )
        if _expr_contains_floor_div(instruction.source_expression):
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_MATH_FLOOR_DIV,
                    severity=_route_severity(CLK_MATH_FLOOR_DIV, mode),
                    message=(
                        f"math() uses floor division (//) which Click does not support "
                        f"at {location_text}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Click has no floor-division operator. "
                        "Use math(a / b, int_dest) instead; "
                        "copying the result to an Int or Dint tag truncates toward zero automatically."
                    ),
                )
            )
        if not mode_info.mixed_families:
            violations = _collect_mode_violations(instruction.source_expression, mode_info.mode)
            if violations:
                location = _instruction_location(base_location, "instruction.expression")
                location_text = _format_location(location)
                unique = list(dict.fromkeys(violations))  # dedupe, preserve order
                names = ", ".join(unique)
                other_mode = "hex" if mode_info.mode == "decimal" else "decimal"
                findings.append(
                    ClickFinding(
                        code=CLK_MATH_FUNC_MODE_MISMATCH,
                        severity=_route_severity(CLK_MATH_FUNC_MODE_MISMATCH, mode),
                        message=(
                            f"math() in {mode_info.mode} mode uses {names} "
                            f"which {'is' if len(unique) == 1 else 'are'} "
                            f"only available in {other_mode} mode at {location_text}."
                        ),
                        location=location_text,
                        suggestion=(
                            f"Move {names} into a separate math() with "
                            f"{other_mode}-family tags, or remap operands to "
                            f"{'WORD (DH)' if other_mode == 'hex' else 'INT/DINT/REAL (DS/DD/DF)'} "
                            f"addresses."
                        ),
                    )
                )
        depth = _expr_paren_depth(instruction.source_expression)
        if depth > _CLICK_MAX_PAREN_DEPTH:
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_MATH_NESTING_DEPTH,
                    severity=_route_severity(CLK_MATH_NESTING_DEPTH, mode),
                    message=(
                        f"math() expression has {depth} levels of parenthetical nesting "
                        f"at {location_text}; Click allows at most "
                        f"{_CLICK_MAX_PAREN_DEPTH}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Break the expression into intermediate math() steps "
                        "with temporary tags to reduce nesting depth."
                    ),
                )
            )

    if instruction_type in {"FunctionCallInstruction", "EnabledFunctionCallInstruction"}:
        location_text = _format_location(base_location)
        findings.append(
            ClickFinding(
                code=CLK_FUNCTION_CALL_NOT_PORTABLE,
                severity=_route_severity(CLK_FUNCTION_CALL_NOT_PORTABLE, mode),
                message=(
                    f"{instruction_type} is not Click-portable at {location_text}. "
                    "Click execution cannot run arbitrary Python callables."
                ),
                location=location_text,
                suggestion=(
                    "Replace run_function/run_enabled_function with Click-portable instructions "
                    "(copy/math/timer/counter/send/receive)."
                ),
            )
        )
    return findings


_MAX_CLICK_FORLOOP_DEPTH = 3


def _evaluate_forloop_structure(program: Any, mode: ValidationMode) -> list[ClickFinding]:
    """CLICK's For/Next rules that pyrung's core does not enforce.

    Loops nest at most three deep counting the loops of subroutines called
    inside a loop (C010D); only the outermost For may be one-shot; RETURN may
    not sit between For and Next.
    """
    findings: list[ClickFinding] = []
    scopes: dict[str | None, list[Any]] = {None: list(program.rungs)}
    scopes.update(program.subroutines)

    def _label(sub: str | None, rung_index: int) -> str:
        scope = "main" if sub is None else f"subroutine '{sub}'"
        return f"{scope} rung {rung_index + 1}"

    # Deepest loop nesting each scope reaches, counting into its calls.
    depth_memo: dict[str | None, int] = {}

    def _loop_depth(instructions: Any, depth: int, active: frozenset[str]) -> int:
        deepest = depth
        for instr in instructions:
            if isinstance(instr, ForLoopInstruction):
                deepest = max(deepest, _loop_depth(instr.instructions, depth + 1, active))
            elif isinstance(instr, CallInstruction) and depth > 0:
                deepest = max(deepest, depth + _scope_depth(instr.subroutine_name, active))
        return deepest

    def _scope_depth(sub: str | None, active: frozenset[str] = frozenset()) -> int:
        if sub in depth_memo:
            return depth_memo[sub]
        if sub is not None and sub in active:
            return 0
        inner = active if sub is None else active | {sub}
        deepest = 0
        for rung in scopes.get(sub, ()):
            deepest = max(deepest, _loop_depth(_rung_instructions(rung), 0, inner))
        depth_memo[sub] = deepest
        return deepest

    for sub, rungs in scopes.items():
        for rung_index, rung in enumerate(rungs):
            location = _label(sub, rung_index)
            for loop in _rung_instructions(rung):
                if not isinstance(loop, ForLoopInstruction):
                    continue
                depth = _loop_depth([loop], 0, frozenset())
                if depth > _MAX_CLICK_FORLOOP_DEPTH:
                    findings.append(
                        ClickFinding(
                            code=CLK_FORLOOP_NESTING_DEPTH,
                            severity=_route_severity(CLK_FORLOOP_NESTING_DEPTH, mode),
                            message=(
                                f"forloop at {location} nests {depth} deep, counting the "
                                "loops of subroutines it calls; CLICK allows 3."
                            ),
                            location=location,
                            suggestion="Flatten a level, or move the inner loop out of the call.",
                        )
                    )
                for inner in _nested_loops(loop):
                    if inner._oneshot:
                        findings.append(
                            ClickFinding(
                                code=CLK_FORLOOP_INNER_ONESHOT,
                                severity=_route_severity(CLK_FORLOOP_INNER_ONESHOT, mode),
                                message=(
                                    f"forloop(..., oneshot=True) nested in the loop at "
                                    f"{location}; CLICK allows one-shot only on the outermost For."
                                ),
                                location=location,
                                suggestion="Drop oneshot= from the inner loop.",
                            )
                        )
                if any(isinstance(instr, ReturnInstruction) for instr in _flat_loop(loop)):
                    findings.append(
                        ClickFinding(
                            code=CLK_FORLOOP_RETURN_INSIDE,
                            severity=_route_severity(CLK_FORLOOP_RETURN_INSIDE, mode),
                            message=(
                                f"return_early() inside the forloop at {location}; CLICK does "
                                "not allow RETURN between For and Next."
                            ),
                            location=location,
                            suggestion="Use break_loop() to leave the loop, then return after it.",
                        )
                    )
    return findings


# The memory types a CLICK For takes its count from (besides a constant).
_FORLOOP_COUNT_BANKS = ("DS", "DD", "TD", "CTD", "SD")


def _evaluate_forloop_count(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """A For count takes a constant, DS, DD, TD, CTD or SD; a pasted DF stops the PLC."""
    if not isinstance(instruction, ForLoopInstruction):
        return []
    count = instruction.count
    if not isinstance(count, (Tag, IndirectRef, IndirectExprRef)):
        return []
    resolution = resolver.resolve_operand_slots(count)
    banks = list(dict.fromkeys(slot.memory_type for slot in resolution.slots))
    bad = [bank for bank in banks if bank not in _FORLOOP_COUNT_BANKS]
    if not bad:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.count"))
    return [
        ClickFinding(
            code=CLK_FORLOOP_COUNT_BANK,
            severity=_route_severity(CLK_FORLOOP_COUNT_BANK, mode),
            message=(
                f"forloop count at {location_text} is in {', '.join(bad)}; CLICK takes a "
                "For count from a constant, DS, DD, TD, CTD or SD, and a pasted DF count "
                "stops the PLC."
            ),
            location=location_text,
            suggestion="Copy the count into a DS or DD tag and loop on that.",
        )
    ]


def _collect_rands(expr: Any, out: list[RandIntExpr], seen: set[int]) -> None:
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, RandIntExpr):
        out.append(expr)
    for child in vars(expr).values():
        _collect_rands(child, out, seen)


def _collect_plain_arg_sites(
    expr: Any, out: list[tuple[str, tuple[Any, ...]]], seen: set[int]
) -> None:
    """Each RAND, LSH, RSH, LRO and RRO in *expr*, with its arguments (``<<``/``>>`` as LSH/RSH)."""
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, RandIntExpr):
        out.append(("RAND", (expr.lo, expr.hi)))
    elif isinstance(expr, ShiftFuncExpr) and expr.name in _HEX_ONLY_SHIFT_FUNCS:
        out.append((expr.name.upper(), (expr.value, expr.count)))
    elif isinstance(expr, BinaryExpr) and expr.symbol in ("<<", ">>"):
        out.append((_CLICK_OP_NAME[expr.symbol], (expr.left, expr.right)))
    for child in vars(expr).values():
        _collect_plain_arg_sites(child, out, seen)


def _is_bad_bound_type(bound: Any) -> bool:
    """RAND takes an integer constant or an Int or Dint tag: not a DF, a float, a bit or a Word."""
    if isinstance(bound, TagExpr):
        return bound.tag.type not in (TagType.INT, TagType.DINT)
    return isinstance(bound, LiteralExpr) and isinstance(bound.value, (float, bool))


def _evaluate_rand_bounds(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """CLICK's editor takes only an integer constant or an Int or Dint tag as a RAND bound,
    refuses equal constant bounds, and an expression as an argument of RAND, LSH, RSH, LRO or RRO."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.expression"))
    findings: list[ClickFinding] = []
    sites: list[tuple[str, tuple[Any, ...]]] = []
    _collect_plain_arg_sites(instruction.source_expression, sites, set())
    for name, args in sites:
        if all(isinstance(arg, (TagExpr, LiteralExpr)) for arg in args):
            continue
        findings.append(
            ClickFinding(
                code=CLK_MATH_ARG_NOT_PLAIN,
                severity=_route_severity(CLK_MATH_ARG_NOT_PLAIN, mode),
                message=(
                    f"{name} at {location_text} has an expression as an argument; CLICK takes "
                    f"only a tag or a constant there (its editor refuses RAND ( DD1 / 2 , 10 ))."
                ),
                location=location_text,
                suggestion="Compute the argument into a tag in a math() above, and pass the tag.",
            )
        )
    rands: list[RandIntExpr] = []
    _collect_rands(instruction.source_expression, rands, set())
    for rand in rands:
        if any(_is_bad_bound_type(b) for b in (rand.lo, rand.hi)):
            findings.append(
                ClickFinding(
                    code=CLK_RAND_BOUND_TYPE,
                    severity=_route_severity(CLK_RAND_BOUND_TYPE, mode),
                    message=(
                        f"randint() at {location_text} has a bound that is not an integer "
                        "constant or an Int or Dint tag; the CLICK editor takes no other."
                    ),
                    location=location_text,
                    suggestion="Use integer constants, or copy the bound into an Int or Dint tag.",
                )
            )
        elif (
            isinstance(rand.lo, LiteralExpr)
            and isinstance(rand.hi, LiteralExpr)
            and rand.lo.value == rand.hi.value
        ):
            findings.append(
                ClickFinding(
                    code=CLK_RAND_EQUAL_BOUNDS,
                    severity=_route_severity(CLK_RAND_EQUAL_BOUNDS, mode),
                    message=(
                        f"randint({rand.lo.value}, {rand.hi.value}) at {location_text} has "
                        "equal constant bounds; the CLICK editor refuses them."
                    ),
                    location=location_text,
                    suggestion="Use a plain constant instead of a one-value draw.",
                )
            )
    return findings


def _collect_wide_constants(
    expr: Any, out: list[int], seen: set[int], *, low: int, high: int, rand_cut: bool
) -> None:
    """Each integer constant in *expr* outside *low*..*high*; a RAND's bounds only if *rand_cut*."""
    if isinstance(expr, bool):
        return
    if isinstance(expr, int):
        if not low <= expr <= high:
            out.append(expr)
        return
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, LiteralExpr):
        _collect_wide_constants(expr.value, out, seen, low=low, high=high, rand_cut=rand_cut)
        return
    if isinstance(expr, RandIntExpr) and not rand_cut:
        bounds = (expr.lo, expr.hi)
        children = [child for child in vars(expr).values() if all(child is not b for b in bounds)]
    else:
        children = list(vars(expr).values())
    for child in children:
        # Only subexpressions: a constant is a LiteralExpr (an int attribute, such
        # as a RAND's site number, is not one).
        if isinstance(child, Expression):
            _collect_wide_constants(child, out, seen, low=low, high=high, rand_cut=rand_cut)


def _evaluate_wide_constants(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """A Math constant past 16 bits: CLICK cuts it to 16 bits whatever the destination
    (hardware, 2026-11-05), so ``DD1 * 40000`` multiplies by -25536.  A RAND's constant
    bounds are cut only into an Int or a Word."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    hex_mode = instruction.mode == "hex"
    # A hex Math reads a constant as its low 16 bits, unsigned (& 0xFFFF): -1 is 65535.
    low, high = (0, 65535) if hex_mode else (-32768, 32767)
    dest = instruction.dest
    dest_type = (
        dest.type if isinstance(dest, Tag) else getattr(getattr(dest, "block", None), "type", None)
    )
    rand_cut = hex_mode or dest_type == TagType.INT
    wide: list[int] = []
    _collect_wide_constants(
        instruction.source_expression, wide, set(), low=low, high=high, rand_cut=rand_cut
    )
    if not wide:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.expression"))
    listed = ", ".join(str(value) for value in dict.fromkeys(wide))
    return [
        ClickFinding(
            code=CLK_MATH_WIDE_CONSTANT,
            severity=_route_severity(CLK_MATH_WIDE_CONSTANT, mode),
            message=(
                f"math() at {location_text} has a constant past 16 bits ({listed}); a CLICK "
                + (
                    "hex Math reads each constant as its low 16 bits, unsigned, so 70000 "
                    "reads 4464 and -1 reads 65535."
                    if hex_mode
                    else "Math cuts each constant to 16 bits, whatever the destination, so "
                    "40000 reads -25536."
                )
            ),
            location=location_text,
            suggestion=(
                "Write the constant as its 16-bit value, 0..65535 (0h..FFFFh)."
                if hex_mode
                else "Copy the constant into a DD tag in a rung above and use the tag."
            ),
        )
    ]


def _evaluate_nested_calls(program: Any, mode: ValidationMode) -> list[ClickFinding]:
    """CLICK has no nested subroutine calls: a ``call()`` may sit in main only."""
    findings: list[ClickFinding] = []
    for sub_name, rungs in program.subroutines.items():
        for rung_index, rung in enumerate(rungs):
            instructions: list[Any] = []
            for instr in _rung_instructions(rung):
                instructions.append(instr)
                if isinstance(instr, ForLoopInstruction):
                    instructions.extend(_flat_loop(instr))
            for instr in instructions:
                if not isinstance(instr, CallInstruction):
                    continue
                location = f"subroutine '{sub_name}' rung {rung_index + 1}"
                findings.append(
                    ClickFinding(
                        code=CLK_NESTED_CALL,
                        severity=_route_severity(CLK_NESTED_CALL, mode),
                        message=(
                            f"call('{instr.subroutine_name}') inside subroutine '{sub_name}' "
                            f"at {location}; CLICK does not support nested subroutine calls."
                        ),
                        location=location,
                        suggestion=(
                            "Call both subroutines from the main program, or inline the "
                            "called subroutine's rungs."
                        ),
                    )
                )
    return findings


def _rung_instructions(rung: Any) -> list[Any]:
    instructions = list(rung._instructions)
    for branch in rung._branches:
        instructions.extend(_rung_instructions(branch))
    return instructions


def _flat_loop(loop: ForLoopInstruction) -> list[Any]:
    flat: list[Any] = []
    for instr in loop.instructions:
        flat.append(instr)
        if isinstance(instr, ForLoopInstruction):
            flat.extend(_flat_loop(instr))
    return flat


def _nested_loops(loop: ForLoopInstruction) -> list[ForLoopInstruction]:
    return [instr for instr in _flat_loop(loop) if isinstance(instr, ForLoopInstruction)]
