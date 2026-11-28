"""Structural portability checks for Click validation."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from laddercodec import Code

from pyrung.core.expression import (
    BinaryExpr,
    Expression,
)
from pyrung.core.instruction.control import (
    CallInstruction,
    ForLoopInstruction,
    ReturnInstruction,
    iter_loop_body_rungs,
)
from pyrung.core.rung import dsl_call
from pyrung.core.tag import Tag, TagType
from pyrung.core.validation._common import rung_label
from pyrung.core.validation.walker import ProgramLocation

from .families.math import formula_expression
from .findings import (
    CLK_EXPR_ONLY_IN_MATH,
    CLK_FORLOOP_INNER_ONESHOT,
    CLK_FORLOOP_NESTING_DEPTH,
    CLK_FORLOOP_RETURN_INSIDE,
    CLK_FUNCTION_CALL_NOT_PORTABLE,
    CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
    CLK_IMMEDIATE_EDGE_CONTACT_NOT_ALLOWED,
    CLK_INDIRECT_BLOCK_RANGE_NOT_ALLOWED,
    CLK_INT_TRUTHINESS_EXPLICIT_COMPARE_REQUIRED,
    CLK_MATH_FLOOR_DIV,
    CLK_NESTED_CALL,
    CLK_NOP_NO_COIL,
    CLK_PTR_BLOCK_OFFSET,
    CLK_PTR_CONTEXT_ONLY_COPY,
    CLK_PTR_DS_UNVERIFIED,
    CLK_PTR_EXPR_NOT_ALLOWED,
    CLK_PTR_POINTER_MUST_BE_DS,
    CLK_STATUS_BIT_NOT_PORTABLE,
    CLK_TEXT_WINDOW_NOT_CONTIGUOUS,
    CLK_TILDE_BOOL_CONTACT_ONLY,
    ClickFinding,
    ValidationMode,
    _build_suggestion,
    _route_severity,
    immediate_edge_text,
    immediate_misplaced_text,
    pointer_block_range_text,
    pointer_expression_text,
    pointer_offset_text,
)
from .resolve import (
    _bank_label,
    _format_location,
    _instruction_location,
    _ResolutionCache,
)

if TYPE_CHECKING:
    from pyrung.click.tag_map import TagMap
    from pyrung.core.validation.walker import OperandFact


def _block_offset(block_name: str, tag_map: TagMap) -> int:
    """Return the block's start in its bank minus its logical start, or 0 for an unmapped block."""
    entry = tag_map._block_entry_by_name(block_name) if block_name else None
    if entry is None:
        return 0
    try:
        return tag_map._offset_for(entry.logical)
    except (KeyError, ValueError):
        return 0


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
                    message=(
                        f"The pointer at {location_str} is outside copy(). CLICK accepts a "
                        "pointer only in a Copy."
                    ),
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
                            f"pyrung cannot tell whether pointer '{pointer_name}' at "
                            f"{location_str} is a DS address. CLICK needs a DS pointer."
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
                            f"Pointer '{pointer_name}' at {location_str} is mapped to "
                            f"{memory_type}. CLICK needs a DS pointer."
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(code, fact, tag_map),
                    )
                )
            offset = _block_offset(str(fact.metadata.get("block_name", "")), tag_map)
            if offset:
                block_name = str(fact.metadata.get("block_name", ""))
                findings.append(
                    ClickFinding(
                        code=CLK_PTR_BLOCK_OFFSET,
                        severity=_route_severity(CLK_PTR_BLOCK_OFFSET, mode),
                        message=pointer_offset_text(
                            block_name, pointer_name, offset, f" at {location_str}"
                        ),
                        location=location_str,
                        suggestion=_build_suggestion(CLK_PTR_BLOCK_OFFSET, fact, tag_map),
                    )
                )

    if fact.value_kind == "indirect_expr_ref":
        findings.append(
            ClickFinding(
                code=CLK_PTR_EXPR_NOT_ALLOWED,
                severity=_route_severity(CLK_PTR_EXPR_NOT_ALLOWED, mode),
                message=pointer_expression_text(f" at {location_str}"),
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
                        f"The expression at {location_str} uses ~ to invert bits. In CLICK, "
                        "~ only marks a normally closed contact on a BOOL."
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
                message=(
                    f"The condition at {location_str} tests a number tag on its own. CLICK "
                    "needs a comparison, such as Count != 0."
                ),
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
                message=pointer_block_range_text(f" at {location_str}"),
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


# The coils that take an immediate() target in CLICK.
_IMMEDIATE_COILS = frozenset(
    {"OutInstruction", "LatchInstruction", "ResetInstruction", "ToggleInstruction"}
)


def _evaluate_immediate_usage(
    facts: tuple[OperandFact, ...],
    tag_map: TagMap,
    mode: ValidationMode,
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    condition_types: dict[
        tuple[tuple[str, str | None, int, tuple[int, ...], int | None, str | None], str], str
    ] = {}

    for fact in facts:
        if fact.value_kind != "condition":
            continue
        condition_type = fact.metadata.get("condition_type")
        if not isinstance(condition_type, str):
            continue
        condition_types[(_location_key(fact.location), fact.location.arg_path)] = condition_type

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
                        message=immediate_edge_text(f" at {location_text}"),
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
                    message=immediate_misplaced_text(f" at {location_text}"),
                    location=location_text,
                    suggestion=_build_suggestion(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, fact, tag_map),
                )
            )
            continue

        if loc.instruction_type in _IMMEDIATE_COILS and loc.arg_path == "instruction.target":
            # laddercodec checks the target of an immediate coil.
            continue

        findings.append(
            ClickFinding(
                code=CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED,
                severity=_route_severity(CLK_IMMEDIATE_CONTEXT_NOT_ALLOWED, mode),
                message=immediate_misplaced_text(f" at {location_text}"),
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


# Click formula-pad mode restrictions:
#   decimal: +, -, *, /, MOD, ^, SUM, MAX, MIN, MDN, AVG, ABS,
#            SIN, ASIN, COS, ACOS, TAN, ATAN, SQRT, LOG, LN, RAD, DEG, PI
#   hex:     +, -, *, /, MOD, SUM, MAX, MIN, MDN, AVG, AND, OR, XOR, LSH, RSH, LRO, RRO
# ABS is a UnaryExpr (``abs(x)``), not a MathFuncExpr, so it has its own arm below.


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
                    f"Tag '{name}' at {location_str} is a status bit that only pyrung's "
                    "simulation has. A CLICK timer or counter has no such bit."
                ),
                location=location_str,
                suggestion=(
                    "Use the Done and Acc fields instead, or remove this contact from the "
                    "logic that you export to CLICK. Tests can still read EN, TT, CU and CD "
                    "through .when() and .monitor()."
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
        _walk_rung(rung, rung_label(None, rung_index))

    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, rung_label(sub_name, rung_index))

    return findings


def _text_literal_compares(condition: Any, out: list[tuple[Any, list[Tag]]]) -> None:
    """Append to *out* each ``==``/``!=`` of a Char with a longer literal, with the Chars read.

    pyrung reads the Chars that follow in the block for a text window
    (``txt[1] == "AB"``, see ``text_window_condition``). Any other such compare reads
    its one Char against the whole literal.
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
    """Report a text compare that pyrung and CLICK read differently.

    CLICK compares a longer literal against a TXT and the TXTs after it. pyrung compares
    the Chars that follow in the Char's block. The two agree only where those Chars are
    mapped to the next TXT addresses, one for each character.
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

    # The enable condition of a timer, counter, drum or enabled call repeats the rung's own
    # conditions. Track the compares already seen so each is checked once, where first met.
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
        _walk_rung(rung, rung_label(None, rung_index))
    for sub_name in sorted(program.subroutines):
        for rung_index, rung in enumerate(program.subroutines[sub_name]):
            _walk_rung(rung, rung_label(sub_name, rung_index))
    return findings


def _evaluate_instruction_portability(
    instruction: Any, base_location: ProgramLocation, mode: ValidationMode
) -> list[ClickFinding]:
    findings: list[ClickFinding] = []
    instruction_type = type(instruction).__name__
    if instruction_type == "CalcInstruction":
        if _expr_contains_floor_div(formula_expression(instruction.source_expression)):
            location = _instruction_location(base_location, "instruction.expression")
            location_text = _format_location(location)
            findings.append(
                ClickFinding(
                    code=CLK_MATH_FLOOR_DIV,
                    severity=_route_severity(CLK_MATH_FLOOR_DIV, mode),
                    message=(f"math() at {location_text} uses //. CLICK has no floor division."),
                    location=location_text,
                    suggestion=(
                        "Use math(a / b, int_dest) instead. A copy into an Int or Dint tag "
                        "truncates toward zero."
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
                    f"{dsl_call(instruction)} at {location_text} cannot run on a CLICK, "
                    "because a CLICK cannot run Python code."
                ),
                location=location_text,
                suggestion=(
                    "Replace it with CLICK instructions such as copy(), math(), timers, "
                    "counters, send() or receive()."
                ),
            )
        )

    if instruction_type == "UndecodedInstruction":
        from pyrung.click.undecoded import FIX_TEXT

        location_text = _format_location(base_location)
        code = Code.UNDECODED_RUNG.value
        findings.append(
            ClickFinding(
                code=code,
                severity=_route_severity(code, mode),
                message=f"{instruction.reason(location_text)} {instruction.effect()}",
                location=location_text,
                suggestion=FIX_TEXT,
            )
        )
    return findings


_MAX_CLICK_FORLOOP_DEPTH = 3


def _evaluate_forloop_structure(program: Any, mode: ValidationMode) -> list[ClickFinding]:
    """Report the CLICK For/Next rules that pyrung's core does not enforce.

    Loops nest at most three deep, counting the loops of subroutines called inside a loop
    (C010D). Only the outermost For may be one-shot. RETURN may not sit between For and Next.
    """
    findings: list[ClickFinding] = []
    scopes: dict[str | None, list[Any]] = {None: list(program.rungs)}
    scopes.update(program.subroutines)

    # The deepest loop nesting each scope reaches, counting the subroutines it calls.
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
            location = rung_label(sub, rung_index)
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


def _evaluate_nested_calls(program: Any, mode: ValidationMode) -> list[ClickFinding]:
    """Report a ``call()`` inside a subroutine.

    CLICK has no nested subroutine calls, so a ``call()`` may sit in main only.
    """
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
                location = rung_label(sub_name, rung_index)
                findings.append(
                    ClickFinding(
                        code=CLK_NESTED_CALL,
                        severity=_route_severity(CLK_NESTED_CALL, mode),
                        message=(
                            f"call('{instr.subroutine_name}') is inside subroutine "
                            f"'{sub_name}' at {location}. CLICK does not support nested "
                            "subroutine calls."
                        ),
                        location=location,
                        suggestion=(
                            "Call both subroutines from the main program, or inline the "
                            "called subroutine's rungs."
                        ),
                    )
                )
    return findings


def _evaluate_nop_no_coil(bundle: Any, mode: ValidationMode) -> list[ClickFinding]:
    """Report a NOP behind contacts that no output shares, as CLK_NOP_NO_COIL, once per rung.

    CLICK keeps the rung when it is pasted, but its Syntax Check reports C0003 for it. It
    reports the same for a NOP on a wire or a branch beside a coil. A NOP on a coil's net
    with no contact of its own, or fed by a bare wire, gets no error. The export decides
    which contacts the NOP shares with a coil. This function therefore reads each rung as
    the export draws it, from the *bundle* of the export. Those are CLICK rungs, which can
    differ from pyrung rungs.
    """
    import warnings

    from pyrung.click.codegen.analyzer import _analyze_rungs
    from pyrung.click.codegen.parser import _parse_rows
    from pyrung.click.codegen.shaping import NOP

    findings: list[ClickFinding] = []
    scopes = [
        ("main", bundle.main_rows, False),
        *((f"subroutine '{n}'", r, True) for n, r in bundle.subroutine_rows),
    ]
    for where, rows, in_subroutine in scopes:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("ignore")
                analyzed = _analyze_rungs(_parse_rows(rows), subroutine=in_subroutine)
        except ValueError:
            continue
        number = 0
        reported = False
        for piece in analyzed:
            if not piece.is_continued:
                number += 1
                reported = False
            has_output = any(i.af_token != NOP for i in piece.instructions)
            exposed = any(
                i.af_token == NOP
                and (i.branch_tree is not None or (not has_output and piece.condition_tree))
                for i in piece.instructions
            )
            if not exposed or reported:
                continue
            reported = True
            location = f"{where} CLICK rung {number}"
            findings.append(
                ClickFinding(
                    code=CLK_NOP_NO_COIL,
                    severity=_route_severity(CLK_NOP_NO_COIL, mode),
                    message=(
                        f"The NOP in {location} sits behind contacts that no output shares. "
                        "CLICK's Syntax Check reports this as C0003, 'There is no Coil "
                        "instruction in the rung.'"
                    ),
                    location=location,
                    suggestion="Remove the NOP, or give it an output.",
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
