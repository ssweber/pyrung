"""Dialect-neutral rendering of instructions and conditions as pyrung DSL source.

A finding shows the offending code the way it was written — ``latch(C2000)``,
``copy(120, Speed)``, ``UnitModeCmd < 1 AND UnitModeCmd > 3`` — so an engineer reads
their own ladder, not an abstraction.  Unlike the Click ladder translator
(:mod:`pyrung.click.ladder.instructions`), which resolves every operand through a
``TagMap`` to a physical address and raises on operands it cannot export, this
renderer is neutral: it keys purely on ``Tag.name`` and never fails, so it is safe to
run over any :class:`~pyrung.core.program.Program`.

The companion :func:`caret_of` turns a rendered string and an offending token into a
``(start, length)`` span for the traceback caret.  It marks the first match, so
:func:`argument_span` marks an argument by its position or name instead.
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
from pyrung.core.tag import ImmediateRef, Tag, TagType

_COMPARE_SYMBOLS: dict[type, str] = {
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
    "OnDelayInstruction": "on_delay",
    "OffDelayInstruction": "off_delay",
    "CountUpInstruction": "count_up",
    "CountDownInstruction": "count_down",
    "EventDrumInstruction": "event_drum",
    "TimeDrumInstruction": "time_drum",
    "TimeEventDrumInstruction": "time_event_drum",
    "ToggleInstruction": "toggle",
    "DebounceInstruction": "debounce",
    "FlasherInstruction": "flasher",
    "TimedInstruction": "timed",
    "ShiftInstruction": "shift",
    "SearchInstruction": "search",
    "ModbusSendInstruction": "send",
    "ModbusReceiveInstruction": "receive",
    "PackBitsInstruction": "pack_bits",
    "PackWordsInstruction": "pack_words",
    "PackTextInstruction": "pack_text",
    "UnpackToBitsInstruction": "unpack_to_bits",
    "UnpackToWordsInstruction": "unpack_to_words",
    "ForLoopInstruction": "forloop",
    "FunctionCallInstruction": "run_function",
    "EnabledFunctionCallInstruction": "run_enabled_function",
}


# ---------------------------------------------------------------------------
# Operands
# ---------------------------------------------------------------------------


def operand_name(value: Any) -> str:
    """Render one instruction operand as source text.

    Structure fields use their recoverable Python access path; ordinary tags
    fall back to their canonical ``Tag.name``.
    """
    from pyrung.core.expression import Expression
    from pyrung.core.memory_block import (
        BlockRange,
        IndirectBlockRange,
        IndirectExprRef,
        IndirectRef,
    )
    from pyrung.core.structure import InstanceView

    if isinstance(value, ImmediateRef):
        return operand_name(value.value)
    if isinstance(value, Tag):
        structure = getattr(value, "_pyrung_structure_name", None)
        field = getattr(value, "_pyrung_structure_field", None)
        runtime = getattr(value, "_pyrung_structure_runtime", None)
        if structure and field and runtime is not None:
            if runtime.count == 1:
                return f"{structure}.{field}"
            index = getattr(value, "_pyrung_structure_index", None)
            if index is not None:
                return f"{structure}[{index}].{field}"
        return value.name
    if isinstance(value, BlockRange):
        if hasattr(value, "_instance_start"):
            # A named_array's instance() or instance_select() prints the call.
            text = repr(value)
        else:
            text = f"{value.block.name}{value.start}..{value.block.name}{value.end}"
        return f"{text}.reverse()" if value.reverse_order else text
    if isinstance(value, IndirectRef):
        return f"{value.block.name}[{value.pointer.name}]"
    if isinstance(value, IndirectBlockRange):
        start, end = operand_name(value.start_expr), operand_name(value.end_expr)
        return f"{value.block.name}[{start}..{end}]"
    if isinstance(value, IndirectExprRef):
        return f"{value.block.name}[{render_expr(value.expr)}]"
    if isinstance(value, Expression):
        return render_expr(value)
    if isinstance(value, InstanceView):
        return f"{value._owner.name}[{value._index}]"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, str):
        return repr(value)
    if isinstance(value, (int, float)):
        return str(value)
    # copy() and fill() still build with a list of tags, so a finding can name one.
    if isinstance(value, list):
        return f"[{', '.join(operand_name(item) for item in value)}]"
    if isinstance(value, tuple):
        items = [operand_name(item) for item in value]
        return f"({items[0]},)" if len(items) == 1 else f"({', '.join(items)})"
    return str(value)


def render_expr(expr: Any) -> str:
    """Render an :class:`~pyrung.core.expression.Expression` tree as source."""
    from pyrung.core.expression import (
        BinaryExpr,
        LiteralExpr,
        MathFuncExpr,
        RandIntExpr,
        RangeAggregateExpr,
        ShiftFuncExpr,
        SumExpr,
        TagExpr,
        UnaryExpr,
        format_expr,
    )

    if isinstance(expr, TagExpr):
        return operand_name(expr.tag)
    if isinstance(expr, LiteralExpr):
        value = expr.value
        if isinstance(value, ImmediateRef):
            return f"immediate({operand_name(value.value)})"
        return operand_name(value)
    if isinstance(expr, BinaryExpr):
        return f"({render_expr(expr.left)} {expr.symbol} {render_expr(expr.right)})"
    if isinstance(expr, UnaryExpr):
        if expr.symbol == "abs":
            return f"abs({render_expr(expr.operand)})"
        return f"{expr.symbol}{render_expr(expr.operand)}"
    if isinstance(expr, MathFuncExpr):
        return f"{expr.name}({render_expr(expr.operand)})"
    if isinstance(expr, ShiftFuncExpr):
        return f"{expr.name}({render_expr(expr.value)}, {render_expr(expr.count)})"
    if isinstance(expr, (SumExpr, RangeAggregateExpr)):
        return format_expr(expr)
    if isinstance(expr, RandIntExpr):
        return f"randint({render_expr(expr.lo)}, {render_expr(expr.hi)})"
    return type(expr).__name__


# ---------------------------------------------------------------------------
# Conditions
# ---------------------------------------------------------------------------


def render_condition(cond: Condition) -> str:
    """Render a leaf/compound :class:`Condition` as source text."""
    cond = text_window_of(cond)
    if (
        isinstance(cond, (CompareEq, CompareNe))
        and cond.tag.type is TagType.BOOL
        and isinstance(cond.value, bool)
    ):
        contact_is_true = cond.value if isinstance(cond, CompareEq) else not cond.value
        name = operand_name(cond.tag)
        return name if contact_is_true else f"~{name}"
    if isinstance(cond, (CompareEq, CompareNe, CompareLt, CompareLe, CompareGt, CompareGe)):
        sym = _COMPARE_SYMBOLS[type(cond)]
        return f"{operand_name(cond.tag)} {sym} {operand_name(cond.value)}"
    if isinstance(
        cond,
        (
            IndirectCompareEq,
            IndirectCompareNe,
            IndirectCompareLt,
            IndirectCompareLe,
            IndirectCompareGt,
            IndirectCompareGe,
        ),
    ):
        sym = _COMPARE_SYMBOLS[type(cond)]
        return f"{operand_name(cond.indirect_ref)} {sym} {operand_name(cond.value)}"
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
    from pyrung.core.expression import ExprCompare

    if isinstance(cond, ExprCompare):
        return f"{operand_name(cond.left)} {cond.symbol} {operand_name(cond.right)}"
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

# The operands of each instruction that renders in full, in the order the call takes them.
# Each operand pairs the DSL's name for it with the instruction attribute that holds it.
_OPERANDS: dict[str, tuple[tuple[str, str], ...]] = {
    "CopyInstruction": (("source", "source"), ("target", "dest")),
    "BlockCopyInstruction": (("source", "source"), ("dest", "dest")),
    "FillInstruction": (("value", "value"), ("dest", "dest")),
    "PackBitsInstruction": (("bit_block", "bit_block"), ("dest", "dest")),
    "PackWordsInstruction": (("word_block", "word_block"), ("dest", "dest")),
    "PackTextInstruction": (("source_range", "source_range"), ("dest", "dest")),
    "UnpackToBitsInstruction": (("source", "source"), ("bit_block", "bit_block")),
    "UnpackToWordsInstruction": (("source", "source"), ("word_block", "word_block")),
}


def _converter_text(converter: Any) -> str:
    """Render a ``convert=`` value as source, such as ``to_value`` or ``to_text(exponential=True)``."""
    if converter.mode != "text":
        return f"to_{converter.mode}"
    options = []
    if not converter.suppress_zero:
        options.append("suppress_zero=False")
    if converter.exponential:
        options.append("exponential=True")
    if converter.termination_code is not None:
        written = converter.written_termination_code
        code = converter.termination_code if written is None else written
        options.append(f"termination_code={code!r}")
    return f"to_text({', '.join(options)})"


# The instructions that render with every operand, and not as ``verb(target_name)``.
_IN_FULL = frozenset(
    {
        "OutInstruction",
        "LatchInstruction",
        "ResetInstruction",
        "CalcInstruction",
        "OnDelayInstruction",
        "OffDelayInstruction",
        "CountUpInstruction",
        "CountDownInstruction",
        *_OPERANDS,
    }
)


def renders_in_full(instr: Any) -> bool:
    """Return True if :func:`render_instruction` writes every operand of *instr*."""
    return type(instr).__name__ in _IN_FULL


def _arguments(instr: Any, target_name: str) -> tuple[str, list[tuple[str, str, bool]]]:
    """Return the verb of *instr* and its arguments as ``(name, text, is_keyword)``."""
    itype = type(instr).__name__
    verb = _VERB.get(itype, itype)

    if itype in ("OutInstruction", "LatchInstruction", "ResetInstruction"):
        return verb, [("target", operand_name(instr.target), False)]
    if itype in _OPERANDS:
        # An instruction can hold an operand or a converter in another form than the
        # author wrote. For example, blockcopy() holds a dest tag as the range it writes.
        # The call shows what the author wrote.
        args = [
            (name, operand_name(getattr(instr, f"authored_{attr}", getattr(instr, attr))), False)
            for name, attr in _OPERANDS[itype]
        ]
        converter = getattr(instr, "authored_convert", getattr(instr, "convert", None))
        if converter is not None:
            args.append(("convert", _converter_text(converter), True))
        if getattr(instr, "allow_whitespace", False):
            args.append(("allow_whitespace", "True", True))
        if getattr(instr, "oneshot", False):
            args.append(("oneshot", "True", True))
        return verb, args
    if itype == "CalcInstruction":
        written = getattr(instr, "source_expression", instr.expression)
        return verb, [
            ("expression", render_expr(written), False),
            ("dest", operand_name(instr.dest), False),
        ]
    if itype in (
        "OnDelayInstruction",
        "OffDelayInstruction",
        "CountUpInstruction",
        "CountDownInstruction",
    ):
        owner = operand_name(instr.accumulator)
        if owner.endswith(".Acc"):
            owner = owner[:-4]
        name = "timer" if "Delay" in itype else "counter"
        return verb, [(name, owner, False), ("preset", operand_name(instr.preset), True)]
    return verb, [("target", target_name, False)]


def _call(instr: Any, target_name: str) -> tuple[str, list[tuple[str, int, int, bool]]]:
    """Return *instr* as source, and each argument as ``(name, start, length, is_keyword)``.

    A keyword argument's span covers its value, not ``name=``.
    """
    verb, args = _arguments(instr, target_name)
    code = f"{verb}("
    spans: list[tuple[str, int, int, bool]] = []
    for index, (name, text, keyword) in enumerate(args):
        if index:
            code += ", "
        if keyword:
            code += f"{name}="
        spans.append((name, len(code), len(text), keyword))
        code += text
    return f"{code})", spans


def render_instruction(
    instr: Any,
    target_name: str,
    *,
    caret_token: str | None = None,
) -> tuple[str, tuple[int, int] | None]:
    """Render an instruction as DSL source with a caret over the offending token.

    The caret underlines the first match of *caret_token* (defaulting to *target_name*)
    within the rendered code, or is ``None`` when the token is not found.  *target_name*
    is also the fallback form ``verb(target_name)`` for instruction types whose full
    operand rendering is not spelled out here.  To mark one argument whatever its text,
    use :func:`argument_span`.
    """
    code, _spans = _call(instr, target_name)
    token = caret_token if caret_token is not None else target_name
    return code, caret_of(code, token)


def argument_span(instr: Any, argument: int | str) -> tuple[str, tuple[int, int] | None]:
    """Render *instr* as DSL source, with the ``(start, length)`` of one of its arguments.

    *argument* is the index of a positional argument, or the DSL's name for any argument.
    For ``copy(Count, Count)``, ``1`` and ``"target"`` both mark the second ``Count``.
    A keyword argument's span covers its value.  The span is ``None`` when the rendered
    call has no such argument, such as ``"convert"`` on a copy that has no converter.
    """
    code, spans = _call(instr, "")
    if isinstance(argument, int):
        positional = [span for span in spans if not span[3]]
        found = positional[argument] if 0 <= argument < len(positional) else None
    else:
        found = next((span for span in spans if span[0] == argument), None)
    if found is None or not found[2]:
        return code, None
    _name, start, length, _keyword = found
    return code, (start, length)


def caret_of(code: str, token: str | None) -> tuple[int, int] | None:
    """``(start, length)`` of *token* within *code*, or ``None`` if absent."""
    if not token:
        return None
    idx = code.find(token)
    if idx < 0:
        return None
    return idx, len(token)
