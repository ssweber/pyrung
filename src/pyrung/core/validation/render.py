"""Dialect-neutral rendering of instructions and conditions as pyrung DSL source.

A finding shows the offending code the way it was written — ``latch(C2000)``,
``copy(120, Speed)``, ``UnitModeCmd < 1 AND UnitModeCmd > 3`` — so an engineer reads
their own ladder, not an abstraction.  Unlike the Click ladder translator
(:mod:`pyrung.click.ladder.instructions`), which resolves every operand through a
``TagMap`` to a physical address and raises on operands it cannot export, this
renderer is neutral: it keys purely on ``Tag.name`` and never fails, so it is safe to
run over any :class:`~pyrung.core.program.Program`.

The companion :func:`caret_of` turns a rendered string and an offending token into a
``(start, length)`` span for the traceback caret.
"""

from __future__ import annotations

from typing import Any

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
    IntTruthyCondition,
    NormallyClosedCondition,
    RisingEdgeCondition,
)
from pyrung.core.tag import ImmediateRef, Tag

_COMPARE_SYMBOLS: dict[type, str] = {
    CompareEq: "==",
    CompareNe: "!=",
    CompareLt: "<",
    CompareLe: "<=",
    CompareGt: ">",
    CompareGe: ">=",
}

# type(instruction).__name__ -> DSL verb.  Only the verb is needed for the fallback
# form ``verb(target)``; the common instructions below render their full operands.
_VERB: dict[str, str] = {
    "OutInstruction": "out",
    "LatchInstruction": "latch",
    "ResetInstruction": "reset",
    "CopyInstruction": "copy",
    "BlockCopyInstruction": "blockcopy",
    "FillInstruction": "fill",
    "CalcInstruction": "math",
    "OnDelayInstruction": "ondelay",
    "OffDelayInstruction": "offdelay",
    "CountUpInstruction": "countup",
    "CountDownInstruction": "countdown",
    "EventDrumInstruction": "eventdrum",
    "TimeDrumInstruction": "timedrum",
    "ShiftInstruction": "shift",
    "SearchInstruction": "search",
    "ModbusSendInstruction": "send",
    "ModbusReceiveInstruction": "receive",
    "PackBitsInstruction": "pack_bits",
    "PackWordsInstruction": "pack_words",
    "PackTextInstruction": "pack_text",
    "UnpackToBitsInstruction": "unpack_bits",
    "UnpackToWordsInstruction": "unpack_words",
}


# ---------------------------------------------------------------------------
# Operands
# ---------------------------------------------------------------------------


def operand_name(value: Any) -> str:
    """Render one instruction operand as source text, keyed on ``Tag.name``."""
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )

    if isinstance(value, ImmediateRef):
        return operand_name(value.value)
    if isinstance(value, Tag):
        return value.name
    if isinstance(value, BlockRange):
        return f"{value.block.name}{value.start}..{value.block.name}{value.end}"
    if isinstance(value, IndirectRef):
        return f"{value.block.name}[{value.pointer.name}]"
    if isinstance(value, IndirectBlockRange):
        return f"{value.block.name}[{value.start_expr}..{value.end_expr}]"
    if isinstance(value, IndirectExprRef):
        return f"{value.block.name}[{value.expr}]"
    if isinstance(value, Expression):
        return render_expr(value)
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float)):
        return str(value)
    return str(value)


def render_expr(expr: Any) -> str:
    """Render an :class:`~pyrung.core.expression.Expression` tree as source."""
    from pyrung.core.expression import BinaryExpr, LiteralExpr, TagExpr, UnaryExpr

    if isinstance(expr, TagExpr):
        return expr.tag.name
    if isinstance(expr, LiteralExpr):
        return str(expr.value)
    if isinstance(expr, BinaryExpr):
        return f"({render_expr(expr.left)} {expr.symbol} {render_expr(expr.right)})"
    if isinstance(expr, UnaryExpr):
        return f"{expr.symbol}{render_expr(expr.operand)}"
    return type(expr).__name__


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def render_condition(cond: Condition) -> str:
    """Render a leaf/compound :class:`Condition` as source text."""
    if isinstance(cond, (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)):
        sym = _COMPARE_SYMBOLS[type(cond)]
        return f"{operand_name(cond.tag)} {sym} {operand_name(cond.value)}"
    if isinstance(cond, BitCondition):
        return operand_name(cond.tag)
    if isinstance(cond, NormallyClosedCondition):
        return f"~{operand_name(cond.tag)}"
    if isinstance(cond, IntTruthyCondition):
        return f"{operand_name(cond.tag)} != 0"
    if isinstance(cond, RisingEdgeCondition):
        return f"rise({operand_name(cond.tag)})"
    if isinstance(cond, FallingEdgeCondition):
        return f"fall({operand_name(cond.tag)})"
    if isinstance(cond, AnyCondition):
        return f"Or({', '.join(render_condition(c) for c in cond.conditions)})"
    if isinstance(cond, AllCondition):
        return f"And({', '.join(render_condition(c) for c in cond.conditions)})"
    return type(cond).__name__


def render_rung_args(conds: Any) -> str:
    """Render a rung's conditions as its comma-separated ``rung(...)`` arguments.

    This is how a rung is written — ``rung(a, b)`` — so the top level is a comma
    join; ``And()`` / ``Or()`` appear only for *nested* combinators (via
    :func:`render_condition`).
    """
    return ", ".join(render_condition(c) for c in conds)


def with_rung_line(conds: Any) -> str:
    """The ``with rung(...):`` header line for a rung's conditions."""
    return f"with rung({render_rung_args(conds)}):"


# ---------------------------------------------------------------------------
# Instructions
# ---------------------------------------------------------------------------


def render_instruction(
    instr: Any,
    target_name: str,
    *,
    caret_token: str | None = None,
) -> tuple[str, tuple[int, int] | None]:
    """Render an instruction as DSL source with a caret over the offending token.

    The caret underlines *caret_token* (defaulting to *target_name*) within the
    rendered code, or is ``None`` when the token is not found.  *target_name* is
    also the fallback form ``verb(target_name)`` for instruction types whose full
    operand rendering is not spelled out here.
    """
    itype = type(instr).__name__

    if itype == "OutInstruction":
        code = f"out({operand_name(instr.target)})"
    elif itype == "LatchInstruction":
        code = f"latch({operand_name(instr.target)})"
    elif itype == "ResetInstruction":
        code = f"reset({operand_name(instr.target)})"
    elif itype in ("CopyInstruction", "BlockCopyInstruction"):
        verb = _VERB[itype]
        code = f"{verb}({operand_name(instr.source)}, {operand_name(instr.dest)})"
    elif itype == "FillInstruction":
        code = f"fill({operand_name(instr.value)}, {operand_name(instr.dest)})"
    elif itype == "CalcInstruction":
        code = f"math({operand_name(instr.dest)}, {render_expr(instr.expression)})"
    else:
        verb = _VERB.get(itype, itype)
        code = f"{verb}({target_name})"

    token = caret_token if caret_token is not None else target_name
    return code, caret_of(code, token)


def caret_of(code: str, token: str | None) -> tuple[int, int] | None:
    """``(start, length)`` of *token* within *code*, or ``None`` if absent."""
    if not token:
        return None
    idx = code.find(token)
    if idx < 0:
        return None
    return idx, len(token)
