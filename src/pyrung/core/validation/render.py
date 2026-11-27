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

import inspect
from collections.abc import Sequence
from dataclasses import dataclass
from enum import Enum
from functools import cache
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
from pyrung.core.instruction.operands import PARSED, held
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

    if isinstance(value, PARSED):
        value = held(value)
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
        left = render_expr(expr.left)
        if expr.symbol == "**" and left.startswith(("-", "+", "~")):
            # Python applies ** before a sign on its left, so -A ** 2 reads as -(A ** 2).
            left = f"({left})"
        return f"({left} {expr.symbol} {render_expr(expr.right)})"
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


# The text of one argument: a str, or the text of each item of a list.
ArgumentText = str | Sequence[str]


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


# The instructions that render with every operand, and not as ``verb(target_name)``, besides
# those that declare ``_call_operands``.
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
    }
)


def _call_operands(instr: Any) -> tuple[tuple[str, str | tuple[str, ...]], ...] | None:
    """Return the operands that *instr* declares for its call, or None if it declares none."""
    return getattr(type(instr), "_call_operands", None)


def renders_in_full(instr: Any) -> bool:
    """Return True if :func:`render_instruction` writes every operand of *instr*."""
    return type(instr).__name__ in _IN_FULL or _call_operands(instr) is not None


def _value_text(value: Any) -> ArgumentText:
    """Return the text of an operand, or the text of each item of a list."""
    if isinstance(value, PARSED):
        value = held(value)
    if isinstance(value, list):
        return [operand_name(item) for item in value]
    return operand_name(value)


def _operand_text(instr: Any, name: str, attribute: str | tuple[str, ...]) -> ArgumentText:
    """Return the text of the argument *name*, which the attribute or attributes hold."""
    if isinstance(attribute, str):
        # An instruction can hold an operand in another form than the author wrote. For
        # example, blockcopy() holds a dest tag as the range it writes. The call shows
        # what the author wrote.
        return _value_text(getattr(instr, f"authored_{attribute}", getattr(instr, attribute)))
    compose = getattr(instr, "_call_argument", None)
    if compose is not None:
        return compose(name, operand_name)
    # Without its own text, an argument that holds several attributes is a structure, such
    # as a timer's UDT. Each attribute holds one field of it.
    owners = {operand_name(getattr(instr, field)).rpartition(".")[0] for field in attribute}
    if len(owners) != 1 or "" in owners:
        raise TypeError(
            f"{type(instr).__name__}.{name} holds {', '.join(attribute)}, which are not "
            "the fields of one structure. Give the class a _call_argument()."
        )
    return owners.pop()


@cache
def _defaults(cls: type) -> dict[str, Any]:
    """Return the default of each parameter of the constructor of *cls* that has one."""
    try:
        parameters = inspect.signature(cls).parameters.values()
    except (TypeError, ValueError):
        return {}
    return {p.name: p.default for p in parameters if p.default is not inspect.Parameter.empty}


def _is_default(cls: type, name: str, value: Any) -> bool:
    """Return True if *value* is the default of the constructor's parameter *name*.

    The call leaves such a keyword out, because the author most likely left it out too. A
    default given as text, such as ``unit="Tms"``, matches the enum member that it names.
    """
    defaults = _defaults(cls)
    if name not in defaults:
        return False
    default = defaults[name]
    if isinstance(value, Enum) and isinstance(default, str):
        return default in (value.name, value.value)
    # A tag's == builds a condition, so only plain values compare.
    return isinstance(value, (bool, int, float, str, Enum)) and value == default


def _keyword_text(instr: Any, name: str) -> str | None:
    """Return the text of the flag or option *name*, or None if the call leaves it out."""
    value = getattr(instr, f"authored_{name}", getattr(instr, name, None))
    if value is None or _is_default(type(instr), name, value):
        return None
    if name == "convert":
        return _converter_text(value)
    if isinstance(value, Enum):
        # The author writes an option such as a timer's unit as text, and the class holds
        # it as an enum member.
        return operand_name(value.value)
    if isinstance(value, int) and not isinstance(value, bool):
        # A flag such as oneshot shows as True, whether the author passed True or 1. A
        # keyword that holds a number is in _call_keyword_operands, which shows the number.
        return "True" if value else "False"
    return operand_name(value)


def _special_arguments(instr: Any) -> tuple[str, list[tuple[str, ArgumentText, str]]] | None:
    """Return the call of an instruction that the renderer writes in its own way, or None.

    These run before the declared calls in :func:`render_instruction`. A family that
    declares one of these calls keeps the frame of every other finding, and the family's
    own findings show the declared call through :func:`declared_call`. The change after the
    last family deletes these special cases.
    """
    itype = type(instr).__name__
    verb = _VERB.get(itype, itype)
    if itype in ("OutInstruction", "LatchInstruction", "ResetInstruction"):
        return verb, [("target", operand_name(instr.target), "positional")]
    if itype == "CalcInstruction":
        written = held(instr.source_expression)
        return verb, [
            ("expression", render_expr(written), "positional"),
            ("dest", operand_name(instr.dest), "positional"),
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
        return verb, [
            (name, owner, "positional"),
            ("preset", operand_name(instr.preset), "keyword"),
        ]
    return None


def _declared_arguments(instr: Any) -> tuple[str, list[tuple[str, ArgumentText, str]]] | None:
    """Return the call that *instr* declares, or None if it declares none.

    The call holds the positional arguments, then the keywords that hold an operand, then
    the flags and options, then the chained calls.
    """
    operands = _call_operands(instr)
    if operands is None:
        return None
    cls = type(instr)
    args = [(name, _operand_text(instr, name, attr), "positional") for name, attr in operands]
    for name, attribute in cls._call_keyword_operands:
        value = getattr(instr, f"authored_{attribute}", getattr(instr, attribute))
        if value is not None:
            args.append((name, _value_text(value), "keyword"))
    for name in cls._call_keywords:
        text = _keyword_text(instr, name)
        if text is not None:
            args.append((name, text, "keyword"))
    for name, attribute in cls._call_chained:
        condition = getattr(instr, attribute)
        if condition is not None:
            args.append((name, render_condition(condition), "chained"))
    return _VERB.get(cls.__name__, cls.__name__), args


def _arguments(instr: Any, target_name: str) -> tuple[str, list[tuple[str, ArgumentText, str]]]:
    """Return the verb of *instr* and its arguments as ``(name, text, kind)``.

    The kind is ``"positional"``, ``"keyword"`` or ``"chained"``.
    """
    found = _special_arguments(instr) or _declared_arguments(instr)
    if found is not None:
        return found
    itype = type(instr).__name__
    return _VERB.get(itype, itype), [("target", target_name, "positional")]


@dataclass(frozen=True)
class Call:
    """An instruction as its author's call, with the place of each argument in the text."""

    text: str
    # Each argument as ``(name, start, length, kind)``. The kind is ``"positional"``,
    # ``"keyword"`` or ``"chained"``. A keyword argument's span covers its value, not
    # ``name=``. A chained call's span covers its condition, not ``.name()``.
    arguments: tuple[tuple[str, int, int, str], ...]
    # The ``(start, length)`` of each item of a list argument, such as each output of
    # ``outputs=[Out1, X9]``, in the order of ``arguments``. An argument that is not a list
    # has no items, and a call without this field has no items at all.
    items: tuple[tuple[tuple[int, int], ...], ...] = ()

    def span(self, argument: int | str, item: int | None = None) -> tuple[int, int] | None:
        """Return the ``(start, length)`` of *argument*, or None if the call does not show it.

        *argument* is the index of a positional argument, or the name of any argument or
        chained call. With *item*, return the span of that item of a list argument. A call
        that shows the list without its items gives the span of the whole list.
        """
        if isinstance(argument, int):
            positional = [i for i, arg in enumerate(self.arguments) if arg[3] == "positional"]
            index = positional[argument] if 0 <= argument < len(positional) else None
        else:
            index = next((i for i, arg in enumerate(self.arguments) if arg[0] == argument), None)
        if index is None or not self.arguments[index][2]:
            return None
        items = self.items[index] if index < len(self.items) else ()
        if item is not None and 0 <= item < len(items):
            return items[item]
        return self.arguments[index][1], self.arguments[index][2]


def call_of(verb: str, arguments: Sequence[tuple[str, ArgumentText, str]]) -> Call:
    """Return the call ``verb(...)`` of *arguments*, each given as ``(name, text, kind)``.

    The kinds are those of :attr:`Call.arguments`. The positional and keyword arguments go
    inside the brackets in the order given, and the chained calls follow. An argument whose
    text is a list of item texts shows as ``[a, b]``, and the call keeps the span of each
    item.
    """
    code = f"{verb}("
    spans: list[tuple[str, int, int, str]] = []
    items: list[tuple[tuple[int, int], ...]] = []

    def add(name: str, text: ArgumentText, kind: str) -> None:
        nonlocal code
        start = len(code)
        if isinstance(text, str):
            code += text
            items.append(())
        else:
            places = []
            code += "["
            for index, item in enumerate(text):
                if index:
                    code += ", "
                places.append((len(code), len(item)))
                code += item
            code += "]"
            items.append(tuple(places))
        spans.append((name, start, len(code) - start, kind))

    inside = [arg for arg in arguments if arg[2] != "chained"]
    for index, (name, text, kind) in enumerate(inside):
        if index:
            code += ", "
        if kind == "keyword":
            code += f"{name}="
        add(name, text, kind)
    code += ")"
    for name, text, kind in arguments:
        if kind == "chained":
            code += f".{name}("
            add(name, text, kind)
            code += ")"
    return Call(code, tuple(spans), tuple(items))


def _call(instr: Any, target_name: str) -> Call:
    """Return *instr* as :func:`render_instruction` writes it."""
    return call_of(*_arguments(instr, target_name))


def declared_call(instr: Any) -> Call:
    """Return *instr* as the call that its class declares.

    A family's findings show this call. It differs from :func:`render_instruction` only for
    a class that declares the call of a special case, such as a timer with its unit.
    """
    return call_of(*(_declared_arguments(instr) or _arguments(instr, "")))


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
    code = _call(instr, target_name).text
    token = caret_token if caret_token is not None else target_name
    return code, caret_of(code, token)


def argument_span(
    instr: Any, argument: int | str, item: int | None = None
) -> tuple[str, tuple[int, int] | None]:
    """Render *instr* as DSL source, with the ``(start, length)`` of one of its arguments.

    *argument* is the index of a positional argument, or the DSL's name for any argument
    or chained call. For ``copy(Count, Count)``, ``1`` and ``"target"`` both mark the
    second ``Count``. A keyword argument's span covers its value, and a chained call's span
    covers its condition.  The span is ``None`` when the rendered
    call has no such argument, such as ``"convert"`` on a copy that has no converter.
    With *item*, the span covers that item of a list argument.
    """
    call = _call(instr, "")
    return call.text, call.span(argument, item)


def caret_of(code: str, token: str | None) -> tuple[int, int] | None:
    """``(start, length)`` of *token* within *code*, or ``None`` if absent."""
    if not token:
        return None
    idx = code.find(token)
    if idx < 0:
        return None
    return idx, len(token)
