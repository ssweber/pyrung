"""pyrung's checks of math()."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from pyclickplc.addresses import format_address_display

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
from pyrung.core.memory_block import BlockRange
from pyrung.core.tag import ImmediateRef, Tag, TagType
from pyrung.core.validation.render import operand_name
from pyrung.core.validation.walker import ProgramLocation

from ..checks import Check
from ..findings import (
    CLK_MATH_RANGE_MIXED_BANKS,
    ClickFinding,
    ValidationMode,
    _route_severity,
    range_aggregate_advice,
    range_aggregate_problem,
    range_not_consecutive_text,
)
from ..resolve import (
    _format_location,
    _instruction_location,
    _ResolutionCache,
)

if TYPE_CHECKING:
    pass


CLK_MATH_MODE_MIXED = "CLK_MATH_MODE_MIXED"
CLK_MATH_FUNC_MODE_MISMATCH = "CLK_MATH_FUNC_MODE_MISMATCH"
CLK_MATH_NESTING_DEPTH = "CLK_MATH_NESTING_DEPTH"
CLK_MATH_MEDIAN_RANGE_TOO_LARGE = "CLK_MATH_MEDIAN_RANGE_TOO_LARGE"
CLK_MATH_BIT_RANGE_AGGREGATE = "CLK_MATH_BIT_RANGE_AGGREGATE"
CLK_MATH_SUM_BANK_NOT_AVAILABLE = "CLK_MATH_SUM_BANK_NOT_AVAILABLE"
CLK_MATH_BIT_OPERAND = "CLK_MATH_BIT_OPERAND"
CLK_MATH_DEST_BANK = "CLK_MATH_DEST_BANK"
CLK_MATH_ARG_NOT_PLAIN = "CLK_MATH_ARG_NOT_PLAIN"
CLK_MATH_WIDE_CONSTANT = "CLK_MATH_WIDE_CONSTANT"
CLK_RAND_BOUND_TYPE = "CLK_RAND_BOUND_TYPE"
CLK_RAND_EQUAL_BOUNDS = "CLK_RAND_EQUAL_BOUNDS"
CODES = (
    CLK_MATH_MODE_MIXED,
    CLK_MATH_FUNC_MODE_MISMATCH,
    CLK_MATH_NESTING_DEPTH,
    CLK_MATH_MEDIAN_RANGE_TOO_LARGE,
    CLK_MATH_BIT_RANGE_AGGREGATE,
    CLK_MATH_SUM_BANK_NOT_AVAILABLE,
    CLK_MATH_BIT_OPERAND,
    CLK_MATH_DEST_BANK,
    CLK_MATH_ARG_NOT_PLAIN,
    CLK_MATH_WIDE_CONSTANT,
    CLK_RAND_BOUND_TYPE,
    CLK_RAND_EQUAL_BOUNDS,
)


# The banks that CLICK v4's Math dialog accepts as its Result (its Address Picker,
# 2026-11-16). A decimal Math accepts DS, DD, DF, TD, CTD and the writable SD. A hex Math
# accepts DH and YD. The table needs no mode. A Result in the other mode's bank makes a
# mixed Math, and CLK_MATH_MODE_MIXED flags that (``infer_calc_mode`` in calc.py).
_MATH_DEST_BANKS = ("DS", "DD", "DF", "TD", "CTD", "SD", "DH", "YD")


def _evaluate_math_dest_bank(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report CLK_MATH_DEST_BANK, a math() into a bank that CLICK's Math Result rejects.

    Those banks are bit memory, XD and TXT. The write-target check reports an unresolved
    destination and an SD that CLICK does not let a program write.
    """
    if type(instruction).__name__ != "CalcInstruction":
        return []
    resolution = resolver.resolve_operand_slots(instruction.dest)
    banks = list(dict.fromkeys(slot.memory_type for slot in resolution.slots))
    bad = [bank for bank in banks if bank not in _MATH_DEST_BANKS]
    if not bad:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.dest"))
    return [
        ClickFinding(
            code=CLK_MATH_DEST_BANK,
            severity=_route_severity(CLK_MATH_DEST_BANK, mode),
            message=(
                f"math() into {', '.join(bad)} at {location_text}; CLICK's Math takes a "
                "Result in DS, DD, DF, TD, CTD or SD, or DH or YD in a hex Math."
            ),
            location=location_text,
            suggestion=(
                "Store the result in a register of one of those banks, and test or copy "
                "it on from there."
            ),
        )
    ]


# Maximum parenthetical nesting the Click formula pad accepts.
_CLICK_MAX_PAREN_DEPTH = 8


def _is_negative_operand(expr: Any) -> bool:
    """Return True for a right operand that the translator wraps in parentheses for its sign.

    ``DS1 + ( -5 )`` for a literal, ``DS1 * ( 0 - DS2 )`` for a negation.

    A negative literal counts here even in hex mode, where it renders unsigned
    (``FFFFh``) and gets no parentheses. That only overstates the depth.
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

    # Range aggregates: SUM ( a : b ), MAX ( a : b ), ... add one paren level.
    if isinstance(expr, (SumExpr, RangeAggregateExpr)):
        return 1

    # RAND ( lo , hi ) adds one paren level.
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
    """Return Click-native names of operators/functions that violate the Math mode."""
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
# CLICK's object error table requires the range to be ordered Low:High (OBJ-1014) and both
# ends to be in the same address block (OBJ-1015). It limits an MDN range to 1024 bytes
# (OBJ-1013). The exporter writes ``FUNC ( first : last )`` from the range's first and last
# mapped tag. Every member in between must therefore map to the addresses CLICK will read.
# ``range_aggregate_problem`` judges that, for validation and for the export.

_BIT_BANKS = frozenset({"X", "Y", "C", "T", "CT", "SC"})
# SUM reads X, Y, C, T, SC, DS, DD, DF, TD, CTD, SD in decimal mode and DH, XD, YD in hex
# mode. CT and TXT are unavailable in both modes. A bank in the wrong mode needs no check
# here. TagMap ties a tag's type to its bank, and a mixed math() gets CLK_MATH_MODE_MIXED.
_NO_SUM_BANKS = frozenset({"CT", "TXT"})
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
        tags = block_range.tags()
        resolved: list[tuple[str, int]] = []
        for tag in tags:
            slot = resolver.resolve_direct_tag(tag)
            if slot is None or slot.address is None:
                break  # unresolved mappings are reported by the bank checks
            resolved.append((slot.memory_type, slot.address))
        else:
            name = operand_name(block_range)
            judged = range_aggregate_problem(
                name,
                [tag.name for tag in tags],
                [format_address_display(bank, address) for bank, address in resolved],
                reversed_order=block_range.reverse_order,
            )
            if judged is not None:
                code, problem = judged
                findings.append(
                    ClickFinding(
                        code=code,
                        severity=_route_severity(code, mode),
                        message=(
                            f"pyrung cannot export {name}.{kind}() at {location_text} "
                            "as one CLICK range. "
                            f"{range_not_consecutive_text([problem])}"
                        ),
                        location=location_text,
                        suggestion=range_aggregate_advice(problem),
                    )
                )
                if code == CLK_MATH_RANGE_MIXED_BANKS:
                    continue
        if not resolved or len(resolved) < len(tags):
            continue
        bank = resolved[0][0]
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
        if kind == "median":
            size = len(resolved) * _BANK_BYTES.get(bank, 2)
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
    """Append to *out* each Bool tag that *expr* reads as an operand.

    Range aggregates are skipped, because :func:`_evaluate_range_aggregates` checks them,
    SUM over bits included. RAND is skipped too: a bit bound there is ``CLK_RAND_BOUND_TYPE``.
    """
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
    """Report a bit operand in a math() formula as CLK_MATH_BIT_OPERAND.

    CLICK's Math editor refuses a bit in its formula, except in a SUM over a bit range
    (checked in the v4 editor, 2026-11-06).
    """
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
    """Append to *out* each RAND, LSH, RSH, LRO or RRO in *expr* with its arguments.

    A ``<<`` or ``>>`` counts as LSH or RSH.
    """
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


# The banks that CLICK's RAND accepts as a bound (laddercodec editor_grids, 2026-11-16). A decimal
# Math refuses TD, CTD and SD, and a hex Math refuses XD and YD (OBJ-1002).
_RAND_BOUND_BANKS = {"decimal": frozenset({"DS", "DD"}), "hex": frozenset({"DH"})}


# The tag types of those banks. They apply to a bound that the tag map does not map.
_RAND_BOUND_TYPES = {
    "decimal": frozenset({TagType.INT, TagType.DINT}),
    "hex": frozenset({TagType.WORD}),
}


def _is_bad_bound_type(bound: Any, *, hex_mode: bool, resolver: _ResolutionCache) -> bool:
    """Return True if *bound* is not valid for RAND.

    RAND accepts an integer constant, a DS or DD tag, or a DH tag in a hex Math.
    """
    kind = "hex" if hex_mode else "decimal"
    if isinstance(bound, TagExpr):
        slot = resolver.resolve_direct_tag(bound.tag)
        if slot is None:
            return bound.tag.type not in _RAND_BOUND_TYPES[kind]
        return slot.memory_type not in _RAND_BOUND_BANKS[kind]
    return isinstance(bound, LiteralExpr) and isinstance(bound.value, (float, bool))


def _evaluate_rand_bounds(
    instruction: Any,
    base_location: ProgramLocation,
    mode: ValidationMode,
    resolver: _ResolutionCache,
) -> list[ClickFinding]:
    """Report each RAND bound and each Math argument that CLICK's editor refuses.

    The editor accepts only an integer constant or a DS or DD tag as a RAND bound (a DH tag
    in a hex Math). It refuses equal constant bounds. It also refuses an expression as an
    argument of RAND, LSH, RSH, LRO or RRO.
    """
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
    hex_mode = instruction.mode == "hex"
    for rand in rands:
        if any(
            _is_bad_bound_type(b, hex_mode=hex_mode, resolver=resolver) for b in (rand.lo, rand.hi)
        ):
            findings.append(
                ClickFinding(
                    code=CLK_RAND_BOUND_TYPE,
                    severity=_route_severity(CLK_RAND_BOUND_TYPE, mode),
                    message=(
                        f"randint() at {location_text} has a bound that is not an integer "
                        "constant or a DS or DD tag, or a DH tag in a hex Math. The CLICK "
                        "editor takes no other."
                    ),
                    location=location_text,
                    suggestion="Use integer constants, or copy the bound into a DS or DD tag.",
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
    expr: Any, out: list[int | float], seen: set[int], *, low: int, high: int, whole: bool
) -> None:
    """Append to *out* each integer constant in *expr* outside *low*..*high*.

    A RAND's bounds are included. With *whole*, a float constant is checked as its integer.
    A float with a fraction is collected too, because a hex Math takes only whole numbers.
    """
    if isinstance(expr, bool):
        return
    if whole and isinstance(expr, float):
        if not expr.is_integer() or not low <= expr <= high:
            out.append(expr)
        return
    if isinstance(expr, int):
        if not low <= expr <= high:
            out.append(expr)
        return
    if id(expr) in seen or not isinstance(expr, Expression):
        return
    seen.add(id(expr))
    if isinstance(expr, LiteralExpr):
        _collect_wide_constants(expr.value, out, seen, low=low, high=high, whole=whole)
        return
    for child in vars(expr).values():
        # Recurse into subexpressions only. A constant is a LiteralExpr, and an int attribute
        # such as a RAND's site number is not a constant.
        if isinstance(child, Expression):
            _collect_wide_constants(child, out, seen, low=low, high=high, whole=whole)


def _evaluate_wide_constants(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """Report a Math constant that CLICK's editor refuses.

    A decimal Math accepts a constant in a DD's range (2026-11-11). A hex Math accepts a
    whole number in 0h..FFFFh (2026-11-16). pyrung writes -32768..-1 there as its 16 bits.
    So -1 is FFFFh, the value that pyrung's own hex Math reads.
    """
    if type(instruction).__name__ != "CalcInstruction":
        return []
    hex_mode = instruction.mode == "hex"
    low, high = (-32768, 65535) if hex_mode else (-2147483648, 2147483647)
    wide: list[int | float] = []
    _collect_wide_constants(
        instruction.source_expression, wide, set(), low=low, high=high, whole=hex_mode
    )
    if not wide:
        return []
    location_text = _format_location(_instruction_location(base_location, "instruction.expression"))
    listed = ", ".join(str(value) for value in dict.fromkeys(wide))
    if hex_mode:
        message = (
            f"math() at {location_text} has a constant that is not a whole 16-bit value "
            f"({listed}). A CLICK hex Math takes 0h..FFFFh, so the export refuses it."
        )
        suggestion = "Write the constant as a whole 16-bit value, 0..65535 (0h..FFFFh)."
    else:
        message = (
            f"math() at {location_text} has a constant past a DD's range ({listed}). "
            "CLICK's editor refuses it."
        )
        suggestion = "Use a constant in -2147483648..2147483647."
    return [
        ClickFinding(
            code=CLK_MATH_WIDE_CONSTANT,
            severity=_route_severity(CLK_MATH_WIDE_CONSTANT, mode),
            message=message,
            location=location_text,
            suggestion=suggestion,
        )
    ]


def _evaluate_math_mode_mixed(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """Report CLK_MATH_MODE_MIXED, a math() that mixes hex and decimal operands."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    findings: list[ClickFinding] = []
    mode_info = infer_calc_mode(instruction.source_expression, instruction.dest)
    if mode_info.mixed_families:
        location = _instruction_location(base_location, "instruction.expression")
        location_text = _format_location(location)
        findings.append(
            ClickFinding(
                code=CLK_MATH_MODE_MIXED,
                severity=_route_severity(CLK_MATH_MODE_MIXED, mode),
                message=(
                    f"math() at {location_text} mixes WORD operands with other operands. "
                    "CLICK runs a Math in hex or in decimal, not both."
                ),
                location=location_text,
                suggestion=(
                    "Split it into a math() of WORD tags and a math() of the other tags. "
                    "Pass the result from one to the other through a tag."
                ),
            )
        )
    return findings


def _evaluate_math_func_mode(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """Report CLK_MATH_FUNC_MODE_MISMATCH, an operator that only the other Math mode has."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    findings: list[ClickFinding] = []
    mode_info = infer_calc_mode(instruction.source_expression, instruction.dest)
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
                        f"math() at {location_text} is a {mode_info.mode} Math, and "
                        f"{names} {'is' if len(unique) == 1 else 'are'} only available in "
                        f"a {other_mode} Math."
                    ),
                    location=location_text,
                    suggestion=(
                        f"Move {names} into a separate math() whose tags are "
                        f"{'WORD tags in DH' if other_mode == 'hex' else 'INT, DINT or REAL tags in DS, DD or DF'}."
                    ),
                )
            )
    return findings


def _evaluate_math_nesting(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    """Report CLK_MATH_NESTING_DEPTH, brackets nested deeper than CLICK's formula pad takes."""
    if type(instruction).__name__ != "CalcInstruction":
        return []
    findings: list[ClickFinding] = []
    depth = _expr_paren_depth(instruction.source_expression)
    if depth > _CLICK_MAX_PAREN_DEPTH:
        location = _instruction_location(base_location, "instruction.expression")
        location_text = _format_location(location)
        findings.append(
            ClickFinding(
                code=CLK_MATH_NESTING_DEPTH,
                severity=_route_severity(CLK_MATH_NESTING_DEPTH, mode),
                message=(
                    f"math() at {location_text} nests brackets {depth} deep. CLICK "
                    f"allows {_CLICK_MAX_PAREN_DEPTH}."
                ),
                location=location_text,
                suggestion=(
                    "Break the expression into intermediate math() steps "
                    "with temporary tags to reduce nesting depth."
                ),
            )
        )
    return findings


def _run(check: Any) -> Any:
    return lambda instruction, location, context: check(instruction, location, context.mode)


def _resolving(check: Any) -> Any:
    return lambda instruction, location, context: check(
        instruction, location, context.mode, context.resolver
    )


CHECKS = (
    Check("instruction", 15, _run(_evaluate_math_mode_mixed)),
    Check("instruction", 22, _run(_evaluate_math_func_mode)),
    Check("instruction", 24, _run(_evaluate_math_nesting)),
    Check("instruction", 30, _resolving(_evaluate_range_aggregates)),
    Check("instruction", 70, _resolving(_evaluate_rand_bounds)),
    Check("instruction", 80, _run(_evaluate_math_bit_operands)),
    Check("instruction", 90, _run(_evaluate_wide_constants)),
    Check("profile", 80, _resolving(_evaluate_math_dest_bank)),
)

TABLES: dict[str, dict[Any, Any]] = {"WRITE_TARGET_FIELDS": {"CalcInstruction": ("dest",)}}
