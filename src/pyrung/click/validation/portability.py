"""Structural portability checks for Click validation."""

from __future__ import annotations

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
from pyrung.core.memory_block import BlockRange
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation.walker import ProgramLocation

from .findings import (
    CLK_CALC_BIT_RANGE_AGGREGATE,
    CLK_CALC_FLOOR_DIV,
    CLK_CALC_FUNC_MODE_MISMATCH,
    CLK_CALC_MEDIAN_RANGE_TOO_LARGE,
    CLK_CALC_MODE_MIXED,
    CLK_CALC_NESTING_DEPTH,
    CLK_CALC_RANGE_MIXED_BANKS,
    CLK_CALC_RANGE_NOT_CONTIGUOUS,
    CLK_EXPR_ONLY_IN_CALC,
    CLK_FORLOOP_INNER_ONESHOT,
    CLK_FORLOOP_NESTING_DEPTH,
    CLK_FORLOOP_RETURN_INSIDE,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_COIL_TARGET_MUST_BE_Y,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_IMMEDIATE_RANGE_MUST_BE_CONTIGUOUS,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_STATUS_BIT_NOT_PORTABLE,
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
                    code=CLK_EXPR_ONLY_IN_CALC,
                    severity=_route_severity(CLK_EXPR_ONLY_IN_CALC, mode),
                    message=f"Expression used outside calc instruction at {location_str}.",
                    location=location_str,
                    suggestion=_build_suggestion(CLK_EXPR_ONLY_IN_CALC, fact, tag_map),
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
        if isinstance(expr.left, BinaryExpr):
            left_depth += 1
        right_depth = _expr_paren_depth(expr.right)
        if isinstance(expr.right, BinaryExpr):
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
    """Check each calc() range aggregate against CLICK's range rules."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    from pyrung.click.ladder.translator import _RANGE_AGGREGATE_CLICK_NAME

    aggregates: list[tuple[str, BlockRange]] = []
    _collect_range_aggregates(instruction.expression, aggregates, set())
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
                CLK_CALC_RANGE_MIXED_BANKS,
                f"{click_name} range spans more than one address block ({', '.join(banks)})",
                f"Map every member of the {click_name} range into one bank, or split the "
                "calc() into one aggregate per bank.",
            )
            continue
        bank = banks[0]
        if bank in _BIT_BANKS and kind != "sum":
            emit(
                CLK_CALC_BIT_RANGE_AGGREGATE,
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
                CLK_CALC_RANGE_NOT_CONTIGUOUS,
                f"{click_name} range does not map to one contiguous low:high run of {bank} "
                f"addresses (first {bank}{numbers[0]}, last {bank}{numbers[-1]})",
                "Map the range to consecutive addresses in ascending order, and drop "
                ".reverse(): the aggregate does not depend on member order.",
            )
        if kind == "median":
            size = len(numbers) * _BANK_BYTES.get(bank, 2)
            if size > _MDN_MAX_BYTES:
                emit(
                    CLK_CALC_MEDIAN_RANGE_TOO_LARGE,
                    f"MDN range is {size} bytes; CLICK allows at most {_MDN_MAX_BYTES} "
                    "(512 DS/DH or 256 DD/DF registers)",
                    "Shrink the median range, or take the median of smaller ranges in "
                    "separate calc() steps.",
                )
    return findings


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


def _evaluate_instruction_portability(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    if instruction_type == "CalcInstruction":
        mode_info = infer_calc_mode(instruction.expression, instruction.dest)
        if mode_info.mixed_families:
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_CALC_MODE_MIXED,
                    severity=_route_severity(CLK_CALC_MODE_MIXED, mode),
                    message=(
                        "calc() mixes WORD (hex-family) and non-WORD (decimal-family) operands "
                        f"at {location_text}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Split mixed calc math into separate decimal and WORD-only calc() steps, "
                        "or convert through an intermediate tag so each calc() stays one family."
                    ),
                )
            )
        if _expr_contains_floor_div(instruction.expression):
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_CALC_FLOOR_DIV,
                    severity=_route_severity(CLK_CALC_FLOOR_DIV, mode),
                    message=(
                        f"calc() uses floor division (//) which Click does not support "
                        f"at {location_text}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Click has no floor-division operator. "
                        "Use calc(a / b, int_dest) instead; "
                        "copying the result to an Int or Dint tag truncates toward zero automatically."
                    ),
                )
            )
        if not mode_info.mixed_families:
            violations = _collect_mode_violations(instruction.expression, mode_info.mode)
            if violations:
                location = _instruction_location(base_location, "instruction.expression")
                location_text = _format_location(location)
                unique = list(dict.fromkeys(violations))  # dedupe, preserve order
                names = ", ".join(unique)
                other_mode = "hex" if mode_info.mode == "decimal" else "decimal"
                findings.append(
                    ClickFinding(
                        code=CLK_CALC_FUNC_MODE_MISMATCH,
                        severity=_route_severity(CLK_CALC_FUNC_MODE_MISMATCH, mode),
                        message=(
                            f"calc() in {mode_info.mode} mode uses {names} "
                            f"which {'is' if len(unique) == 1 else 'are'} "
                            f"only available in {other_mode} mode at {location_text}."
                        ),
                        location=location_text,
                        suggestion=(
                            f"Move {names} into a separate calc() with "
                            f"{other_mode}-family tags, or remap operands to "
                            f"{'WORD (DH)' if other_mode == 'hex' else 'INT/DINT/REAL (DS/DD/DF)'} "
                            f"addresses."
                        ),
                    )
                )
        depth = _expr_paren_depth(instruction.expression)
        if depth > _CLICK_MAX_PAREN_DEPTH:
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_CALC_NESTING_DEPTH,
                    severity=_route_severity(CLK_CALC_NESTING_DEPTH, mode),
                    message=(
                        f"calc() expression has {depth} levels of parenthetical nesting "
                        f"at {location_text}; Click allows at most "
                        f"{_CLICK_MAX_PAREN_DEPTH}."
                    ),
                    location=location_text,
                    suggestion=(
                        "Break the expression into intermediate calc() steps "
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
                    "(copy/calc/timer/counter/send/receive)."
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
