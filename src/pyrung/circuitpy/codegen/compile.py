"""CircuitPython code generation (feature-complete v1)."""

from __future__ import annotations

from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _DINT_MIN,
    _FAULT_OUT_OF_RANGE_TAG,
    _INT_MAX,
    _INT_MIN,
)
from pyrung.circuitpy.codegen._util import (
    _bool_literal,
    _coil_target_default,
    _indent_body,
    _optional_range_type_name,
    _optional_value_type_name,
    _range_reverse,
    _range_type_name,
    _source_location,
    _static_range_length,
    _subroutine_symbol,
    _value_type_name,
)
from pyrung.circuitpy.codegen.context import CodegenContext
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
from pyrung.core.copy_modifiers import CopyModifier
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
    TagExpr,
    XorExpr,
)
from pyrung.core.instruction import (
    BlockCopyInstruction,
    CalcInstruction,
    CallInstruction,
    CopyInstruction,
    CountDownInstruction,
    CountUpInstruction,
    EnabledFunctionCallInstruction,
    EventDrumInstruction,
    FillInstruction,
    ForLoopInstruction,
    FunctionCallInstruction,
    LatchInstruction,
    OffDelayInstruction,
    OnDelayInstruction,
    OutInstruction,
    PackBitsInstruction,
    PackTextInstruction,
    PackWordsInstruction,
    ResetInstruction,
    ReturnInstruction,
    SearchInstruction,
    ShiftInstruction,
    TimeDrumInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
)
from pyrung.core.memory_block import (
    BlockRange,
    IndirectBlockRange,
    IndirectExprRef,
    IndirectRef,
)
from pyrung.core.rung import Rung as LogicRung
from pyrung.core.tag import Tag, TagType
from pyrung.core.time_mode import TimeUnit


def compile_condition(cond: Condition, ctx: CodegenContext) -> str:
    """Return a Python boolean expression string."""
    if isinstance(cond, BitCondition):
        return f"bool({ctx.symbol_for_tag(cond.tag)})"
    if isinstance(cond, NormallyClosedCondition):
        return f"(not bool({ctx.symbol_for_tag(cond.tag)}))"
    if isinstance(cond, IntTruthyCondition):
        return f"(int({ctx.symbol_for_tag(cond.tag)}) != 0)"
    if isinstance(cond, CompareEq):
        return f"({_compile_value(cond.tag, ctx)} == {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareNe):
        return f"({_compile_value(cond.tag, ctx)} != {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareLt):
        return f"({_compile_value(cond.tag, ctx)} < {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareLe):
        return f"({_compile_value(cond.tag, ctx)} <= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareGt):
        return f"({_compile_value(cond.tag, ctx)} > {_compile_value(cond.value, ctx)})"
    if isinstance(cond, CompareGe):
        return f"({_compile_value(cond.tag, ctx)} >= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, AllCondition):
        parts = [compile_condition(child, ctx) for child in cond.conditions]
        return "(" + " and ".join(parts) + ")" if parts else "True"
    if isinstance(cond, AnyCondition):
        parts = [compile_condition(child, ctx) for child in cond.conditions]
        return "(" + " or ".join(parts) + ")" if parts else "False"
    if isinstance(cond, RisingEdgeCondition):
        ctx.mark_helper("_rise")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = ctx.symbol_for_tag(cond.tag)
        return f'_rise(bool({tag_expr}), bool(_prev.get("{cond.tag.name}", False)))'
    if isinstance(cond, FallingEdgeCondition):
        ctx.mark_helper("_fall")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = ctx.symbol_for_tag(cond.tag)
        return f'_fall(bool({tag_expr}), bool(_prev.get("{cond.tag.name}", False)))'
    if isinstance(cond, IndirectCompareEq):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} == {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareNe):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} != {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareLt):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} < {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareLe):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} <= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareGt):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} > {_compile_value(cond.value, ctx)})"
    if isinstance(cond, IndirectCompareGe):
        return f"({_compile_indirect_value(cond.indirect_ref, ctx)} >= {_compile_value(cond.value, ctx)})"
    if isinstance(cond, ExprCompareEq):
        return f"({compile_expression(cond.left, ctx)} == {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareNe):
        return f"({compile_expression(cond.left, ctx)} != {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareLt):
        return f"({compile_expression(cond.left, ctx)} < {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareLe):
        return f"({compile_expression(cond.left, ctx)} <= {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareGt):
        return f"({compile_expression(cond.left, ctx)} > {compile_expression(cond.right, ctx)})"
    if isinstance(cond, ExprCompareGe):
        return f"({compile_expression(cond.left, ctx)} >= {compile_expression(cond.right, ctx)})"

    raise NotImplementedError(f"Unsupported condition type: {type(cond).__name__}")


def compile_expression(expr: Expression, ctx: CodegenContext) -> str:
    """Return a Python expression string with explicit parentheses."""
    if isinstance(expr, TagExpr):
        return ctx.symbol_for_tag(expr.tag)
    if isinstance(expr, LiteralExpr):
        return repr(expr.value)

    if isinstance(expr, AddExpr):
        return f"({compile_expression(expr.left, ctx)} + {compile_expression(expr.right, ctx)})"
    if isinstance(expr, SubExpr):
        return f"({compile_expression(expr.left, ctx)} - {compile_expression(expr.right, ctx)})"
    if isinstance(expr, MulExpr):
        return f"({compile_expression(expr.left, ctx)} * {compile_expression(expr.right, ctx)})"
    if isinstance(expr, DivExpr):
        return f"({compile_expression(expr.left, ctx)} / {compile_expression(expr.right, ctx)})"
    if isinstance(expr, FloorDivExpr):
        return f"({compile_expression(expr.left, ctx)} // {compile_expression(expr.right, ctx)})"
    if isinstance(expr, ModExpr):
        return f"({compile_expression(expr.left, ctx)} % {compile_expression(expr.right, ctx)})"
    if isinstance(expr, PowExpr):
        return f"({compile_expression(expr.left, ctx)} ** {compile_expression(expr.right, ctx)})"

    if isinstance(expr, NegExpr):
        return f"(-({compile_expression(expr.operand, ctx)}))"
    if isinstance(expr, PosExpr):
        return f"(+({compile_expression(expr.operand, ctx)}))"
    if isinstance(expr, AbsExpr):
        return f"abs({compile_expression(expr.operand, ctx)})"

    if isinstance(expr, AndExpr):
        return f"(int({compile_expression(expr.left, ctx)}) & int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, OrExpr):
        return f"(int({compile_expression(expr.left, ctx)}) | int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, XorExpr):
        return f"(int({compile_expression(expr.left, ctx)}) ^ int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, LShiftExpr):
        return f"(int({compile_expression(expr.left, ctx)}) << int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, RShiftExpr):
        return f"(int({compile_expression(expr.left, ctx)}) >> int({compile_expression(expr.right, ctx)}))"
    if isinstance(expr, InvertExpr):
        return f"(~int({compile_expression(expr.operand, ctx)}))"

    if isinstance(expr, MathFuncExpr):
        allowed = {
            "sqrt",
            "sin",
            "cos",
            "tan",
            "asin",
            "acos",
            "atan",
            "radians",
            "degrees",
            "log10",
            "log",
        }
        if expr.name not in allowed:
            raise TypeError(f"Unsupported expression type: {type(expr).__name__}")
        return f"math.{expr.name}({compile_expression(expr.operand, ctx)})"

    if isinstance(expr, ShiftFuncExpr):
        value = compile_expression(expr.value, ctx)
        count = compile_expression(expr.count, ctx)
        if expr.name == "lsh":
            return f"(int({value}) << int({count}))"
        if expr.name == "rsh":
            return f"(int({value}) >> int({count}))"
        if expr.name == "lro":
            return (
                f"((((int({value}) & 0xFFFF) << (int({count}) % 16)) | "
                f"((int({value}) & 0xFFFF) >> (16 - (int({count}) % 16)))) & 0xFFFF)"
            )
        if expr.name == "rro":
            return (
                f"((((int({value}) & 0xFFFF) >> (int({count}) % 16)) | "
                f"((int({value}) & 0xFFFF) << (16 - (int({count}) % 16)))) & 0xFFFF)"
            )
        raise TypeError(f"Unsupported expression type: {type(expr).__name__}")

    raise TypeError(f"Unsupported expression type: {type(expr).__name__}")


def compile_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr, OutInstruction):
        return _compile_out_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, LatchInstruction):
        lines = [f"{' ' * indent}if {enabled_expr}:"]
        lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
        return lines
    if isinstance(instr, ResetInstruction):
        default_expr = _coil_target_default(instr.target, ctx)
        lines = [f"{' ' * indent}if {enabled_expr}:"]
        lines.extend(_compile_target_write_lines(instr.target, default_expr, ctx, indent + 4))
        return lines
    if isinstance(instr, OnDelayInstruction):
        return _compile_on_delay_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, OffDelayInstruction):
        return _compile_off_delay_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CountUpInstruction):
        return _compile_count_up_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CountDownInstruction):
        return _compile_count_down_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CopyInstruction):
        return _compile_copy_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CalcInstruction):
        return _compile_calc_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, BlockCopyInstruction):
        return _compile_blockcopy_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, FillInstruction):
        return _compile_fill_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, SearchInstruction):
        return _compile_search_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ShiftInstruction):
        return _compile_shift_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, EventDrumInstruction):
        return _compile_event_drum_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, TimeDrumInstruction):
        return _compile_time_drum_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, PackBitsInstruction):
        return _compile_pack_bits_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, PackWordsInstruction):
        return _compile_pack_words_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, PackTextInstruction):
        return _compile_pack_text_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, UnpackToBitsInstruction):
        return _compile_unpack_bits_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, UnpackToWordsInstruction):
        return _compile_unpack_words_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, FunctionCallInstruction):
        return _compile_function_call_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, EnabledFunctionCallInstruction):
        return _compile_enabled_function_call_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, CallInstruction):
        return _compile_call_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ReturnInstruction):
        return _compile_return_instruction(enabled_expr, indent)
    if isinstance(instr, ForLoopInstruction):
        return _compile_for_loop_instruction(instr, enabled_expr, ctx, indent)

    loc = _source_location(instr)
    raise NotImplementedError(f"Unsupported instruction type: {type(instr).__name__} at {loc}")


def compile_rung(rung: LogicRung, fn_name: str, ctx: CodegenContext, indent: int = 0) -> list[str]:
    """Compile one rung into Python source lines."""
    previous = ctx._current_function
    ctx.set_current_function(fn_name)
    try:
        rung_id = ctx.next_name("rung")
        enabled_var = f"_{rung_id}_enabled"
        cond_expr = _compile_condition_group(rung._conditions, ctx)
        lines = [f"{' ' * indent}{enabled_var} = {cond_expr}"]
        lines.extend(
            _compile_rung_items(
                rung=rung,
                enabled_expr=enabled_var,
                ctx=ctx,
                indent=indent,
                scope_key=rung_id,
            )
        )
        return lines
    finally:
        ctx.set_current_function(previous)


def _compile_rung_items(
    rung: LogicRung,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    scope_key: str,
) -> list[str]:
    lines: list[str] = []
    branch_vars: dict[int, str] = {}
    branch_idx = 0
    for item in rung._execution_items:
        if not isinstance(item, LogicRung):
            continue
        local_conditions = item._conditions[item._branch_condition_start :]
        local_expr = _compile_condition_group(local_conditions, ctx)
        branch_var = f"_{scope_key}_branch_{branch_idx}"
        branch_idx += 1
        branch_vars[id(item)] = branch_var
        lines.append(f"{' ' * indent}{branch_var} = ({enabled_expr} and ({local_expr}))")

    branch_scope_idx = 0
    for item in rung._execution_items:
        if isinstance(item, LogicRung):
            branch_var = branch_vars[id(item)]
            child_scope = f"{scope_key}_b{branch_scope_idx}"
            branch_scope_idx += 1
            lines.extend(
                _compile_rung_items(
                    rung=item,
                    enabled_expr=branch_var,
                    ctx=ctx,
                    indent=indent,
                    scope_key=child_scope,
                )
            )
            continue
        lines.extend(compile_instruction(item, enabled_expr, ctx, indent))
    return lines


def _compile_condition_group(conditions: list[Condition], ctx: CodegenContext) -> str:
    if not conditions:
        return "True"
    parts = [compile_condition(cond, ctx) for cond in conditions]
    return " and ".join(parts)


def _compile_out_instruction(
    instr: OutInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    lines: list[str] = []
    sp = " " * indent
    enabled_literal = _bool_literal(enabled_expr)
    if instr.oneshot:
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        if enabled_literal is False:
            lines.append(f"{sp}_mem[{key!r}] = False")
            lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent))
            return lines
        if enabled_literal is True:
            lines.append(f"{sp}if not bool(_mem.get({key!r}, False)):")
            lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
            lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
            return lines
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = False")
        lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent + 4))
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
        return lines

    if enabled_literal is True:
        return _compile_target_write_lines(instr.target, "True", ctx, indent)
    if enabled_literal is False:
        return _compile_target_write_lines(instr.target, "False", ctx, indent)

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
    lines.append(f"{sp}else:")
    lines.extend(_compile_target_write_lines(instr.target, "False", ctx, indent + 4))
    return lines


def _compile_guarded_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    enabled_body: list[str],
    *,
    disabled_body: list[str] | None = None,
) -> list[str]:
    sp = " " * indent
    lines: list[str] = []
    enabled_literal = _bool_literal(enabled_expr)
    if getattr(instr, "oneshot", False):
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        if enabled_literal is False:
            lines.append(f"{sp}_mem[{key!r}] = False")
            if disabled_body:
                lines.extend(f"{sp}{line}" for line in disabled_body)
            return lines
        if enabled_literal is True:
            lines.append(f"{sp}if not bool(_mem.get({key!r}, False)):")
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
            lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
            return lines
        lines.append(f"{sp}if not ({enabled_expr}):")
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = False")
        if disabled_body:
            lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
        lines.append(f"{sp}elif not bool(_mem.get({key!r}, False)):")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
        lines.append(f"{' ' * (indent + 4)}_mem[{key!r}] = True")
        return lines

    if enabled_literal is True:
        lines.extend(f"{sp}{line}" for line in enabled_body)
        return lines
    if enabled_literal is False:
        if disabled_body is not None:
            lines.extend(f"{sp}{line}" for line in disabled_body)
        return lines

    lines.append(f"{sp}if {enabled_expr}:")
    lines.extend(f"{' ' * (indent + 4)}{line}" for line in enabled_body)
    if disabled_body is not None:
        lines.append(f"{sp}else:")
        lines.extend(f"{' ' * (indent + 4)}{line}" for line in disabled_body)
    return lines


def _compile_call_instruction(
    instr: CallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    fn = _subroutine_symbol(instr.subroutine_name)
    enabled_literal = _bool_literal(enabled_expr)
    if enabled_literal is False:
        return []
    if enabled_literal is True:
        return [f"{sp}{fn}()"]
    return [f"{sp}if {enabled_expr}:", f"{sp}    {fn}()"]


def _compile_return_instruction(enabled_expr: str, indent: int) -> list[str]:
    sp = " " * indent
    enabled_literal = _bool_literal(enabled_expr)
    if enabled_literal is False:
        return []
    if enabled_literal is True:
        return [f"{sp}return"]
    return [f"{sp}if {enabled_expr}:", f"{sp}    return"]


def _compile_on_delay_instruction(
    instr: OnDelayInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    frac_key = f"_frac:{instr.accumulator.name}"
    preset = _compile_value(instr.preset, ctx)
    unit_expr = _timer_dt_to_units_expr(instr.unit, "_dt", "_frac")
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    sp = " " * indent
    lines: list[str] = [
        f'{sp}_frac = float(_mem.get("{frac_key}", 0.0))',
    ]
    if instr.reset_condition is not None:
        reset_expr = compile_condition(instr.reset_condition, ctx)
        lines.extend(
            [
                f"{sp}if {reset_expr}:",
                f'{" " * (indent + 4)}_mem["{frac_key}"] = 0.0',
                f"{' ' * (indent + 4)}{done} = False",
                f"{' ' * (indent + 4)}{acc} = 0",
                f"{sp}else:",
            ]
        )
        inner = indent + 4
    else:
        inner = indent
    isp = " " * inner
    lines.extend(
        [
            f"{isp}if {enabled_expr}:",
            f'{" " * (inner + 4)}_dt = float(_mem.get("_dt", 0.0))',
            f"{' ' * (inner + 4)}_acc = int({acc})",
            f"{' ' * (inner + 4)}_dt_units = {unit_expr}",
            f"{' ' * (inner + 4)}_int_units = int(_dt_units)",
            f"{' ' * (inner + 4)}_new_frac = _dt_units - _int_units",
            f"{' ' * (inner + 4)}_acc = min(_acc + _int_units, {_INT_MAX})",
            f"{' ' * (inner + 4)}_preset = int({preset})",
            f'{" " * (inner + 4)}_mem["{frac_key}"] = _new_frac',
            f"{' ' * (inner + 4)}{done} = (_acc >= _preset)",
            f"{' ' * (inner + 4)}{acc} = _acc",
        ]
    )
    if not instr.has_reset:
        lines.extend(
            [
                f"{isp}else:",
                f'{" " * (inner + 4)}_mem["{frac_key}"] = 0.0',
                f"{' ' * (inner + 4)}{done} = False",
                f"{' ' * (inner + 4)}{acc} = 0",
            ]
        )
    return lines


def _compile_off_delay_instruction(
    instr: OffDelayInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    frac_key = f"_frac:{instr.accumulator.name}"
    preset = _compile_value(instr.preset, ctx)
    unit_expr = _timer_dt_to_units_expr(instr.unit, "_dt", "_frac")
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    sp = " " * indent
    return [
        f'{sp}_frac = float(_mem.get("{frac_key}", 0.0))',
        f"{sp}if {enabled_expr}:",
        f'{" " * (indent + 4)}_mem["{frac_key}"] = 0.0',
        f"{' ' * (indent + 4)}{done} = True",
        f"{' ' * (indent + 4)}{acc} = 0",
        f"{sp}else:",
        f'{" " * (indent + 4)}_dt = float(_mem.get("_dt", 0.0))',
        f"{' ' * (indent + 4)}_acc = int({acc})",
        f"{' ' * (indent + 4)}_dt_units = {unit_expr}",
        f"{' ' * (indent + 4)}_int_units = int(_dt_units)",
        f"{' ' * (indent + 4)}_new_frac = _dt_units - _int_units",
        f"{' ' * (indent + 4)}_acc = min(_acc + _int_units, {_INT_MAX})",
        f"{' ' * (indent + 4)}_preset = int({preset})",
        f'{" " * (indent + 4)}_mem["{frac_key}"] = _new_frac',
        f"{' ' * (indent + 4)}{done} = (_acc < _preset)",
        f"{' ' * (indent + 4)}{acc} = _acc",
    ]


def _compile_count_up_instruction(
    instr: CountUpInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    preset = _compile_value(instr.preset, ctx)
    sp = " " * indent
    lines: list[str] = []
    if instr.reset_condition is not None:
        reset_expr = compile_condition(instr.reset_condition, ctx)
        lines.extend(
            [
                f"{sp}if {reset_expr}:",
                f"{' ' * (indent + 4)}{done} = False",
                f"{' ' * (indent + 4)}{acc} = 0",
                f"{sp}else:",
            ]
        )
        inner = indent + 4
    else:
        inner = indent
    isp = " " * inner
    lines.extend(
        [
            f"{' ' * inner}_acc = int({acc})",
            f"{' ' * inner}_delta = 0",
            f"{isp}if {enabled_expr}:",
            f"{' ' * (inner + 4)}_delta += 1",
        ]
    )
    if instr.down_condition is not None:
        down_expr = compile_condition(instr.down_condition, ctx)
        lines.extend(
            [
                f"{isp}if {down_expr}:",
                f"{' ' * (inner + 4)}_delta -= 1",
            ]
        )
    lines.extend(
        [
            f"{' ' * inner}_acc = max({_DINT_MIN}, min({_DINT_MAX}, _acc + _delta))",
            f"{' ' * inner}_preset = int({preset})",
            f"{' ' * inner}{done} = (_acc >= _preset)",
            f"{' ' * inner}{acc} = _acc",
        ]
    )
    return lines


def _compile_count_down_instruction(
    instr: CountDownInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done = ctx.symbol_for_tag(instr.done_bit)
    acc = ctx.symbol_for_tag(instr.accumulator)
    preset = _compile_value(instr.preset, ctx)
    sp = " " * indent
    lines: list[str] = []
    if instr.reset_condition is not None:
        reset_expr = compile_condition(instr.reset_condition, ctx)
        lines.extend(
            [
                f"{sp}if {reset_expr}:",
                f"{' ' * (indent + 4)}{done} = False",
                f"{' ' * (indent + 4)}{acc} = 0",
                f"{sp}else:",
            ]
        )
        inner = indent + 4
    else:
        inner = indent
    isp = " " * inner
    lines.extend(
        [
            f"{' ' * inner}_acc = int({acc})",
            f"{isp}if {enabled_expr}:",
            f"{' ' * (inner + 4)}_acc -= 1",
            f"{' ' * inner}_acc = max({_DINT_MIN}, min({_DINT_MAX}, _acc))",
            f"{' ' * inner}_preset = int({preset})",
            f"{' ' * inner}{done} = (_acc <= -_preset)",
            f"{' ' * inner}{acc} = _acc",
        ]
    )
    return lines


def _compile_copy_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr.source, CopyModifier):
        return _compile_copy_modifier_instruction(instr, enabled_expr, ctx, indent)
    target_type = _value_type_name(instr.target)
    ctx.mark_helper("_store_copy_value_to_type")
    source_expr = _compile_value(instr.source, ctx)
    value_expr = f'_store_copy_value_to_type({source_expr}, "{target_type}")'
    enabled_body = _compile_assignment_lines(instr.target, value_expr, ctx, indent=0)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_copy_modifier_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    modifier = instr.source
    if not isinstance(modifier, CopyModifier):
        raise TypeError("copy modifier compiler requires CopyModifier source")

    stem = ctx.next_name("copymod")
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    mode = modifier.mode
    source_expr = _compile_value(modifier.source, ctx)

    setup, target_kind, target_symbol, target_start_var, target_type = _copy_modifier_target_info(
        instr.target, ctx, stem
    )
    values_var = f"_{stem}_values"
    write_lines = _copy_modifier_write_lines(
        values_var=values_var,
        target_kind=target_kind,
        target_symbol=target_symbol,
        target_start_var=target_start_var,
        fault_body=fault_body,
    )

    enabled_body: list[str] = [*setup]
    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                f"    _copy_text = _text_from_source_value({source_expr})",
                f"    {values_var} = []",
                "    for _copy_char in _copy_text:",
                f'        _copy_numeric = _store_numeric_text_digit(_copy_char, "{mode}")',
                f'        {values_var}.append(_store_copy_value_to_type(_copy_numeric, "{target_type}"))',
                *_indent_body(write_lines, 4),
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "text":
        ctx.mark_helper("_render_text_from_numeric")
        ctx.mark_helper("_termination_char")
        ctx.mark_helper("_store_copy_value_to_type")
        source_type = _optional_value_type_name(modifier.source)
        enabled_body.extend(
            [
                "try:",
                "    _rendered = _render_text_from_numeric(",
                f"        {source_expr},",
                f"        source_type={source_type!r},",
                f"        suppress_zero={modifier.suppress_zero!r},",
                f"        pad={modifier.pad!r},",
                f"        exponential={modifier.exponential!r},",
                "    )",
                f"    _rendered += _termination_char({modifier.termination_code!r})",
                f'    {values_var} = [_store_copy_value_to_type(_ch, "{target_type}") for _ch in _rendered]',
                *_indent_body(write_lines, 4),
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                f"    _copy_char = _ascii_char_from_code(int({source_expr}) & 0xFF)",
                f'    {values_var} = [_store_copy_value_to_type(_copy_char, "{target_type}")]',
                *_indent_body(write_lines, 4),
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    else:
        enabled_body.extend(fault_body)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_calc_instruction(
    instr: CalcInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    value_expr = _compile_value(instr.expression, ctx)
    store_expr = _calc_store_expr("_calc_value", instr.dest.type.name, instr.mode, ctx)
    enabled_body = [
        "try:",
        f"    _calc_value = {value_expr}",
        "except ZeroDivisionError:",
        "    _calc_value = 0",
        "if isinstance(_calc_value, float) and not math.isfinite(_calc_value):",
        "    _calc_value = 0",
        f"{ctx.symbol_for_tag(instr.dest)} = {store_expr}",
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_blockcopy_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr.source, CopyModifier):
        return _compile_blockcopy_modifier_instruction(instr, enabled_expr, ctx, indent)
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("blockcopy")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    enabled_body = [
        *src_setup,
        *dst_setup,
        f"if len({src_indices}) != len({dst_indices}):",
        f'    raise ValueError(f"BlockCopy length mismatch: source has {{len({src_indices})}} elements, dest has {{len({dst_indices})}} elements")',
        f"for _src_idx, _dst_idx in zip({src_indices}, {dst_indices}):",
        f"    _raw = {src_symbol}[_src_idx]",
        f'    {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_raw, "{_range_type_name(instr.dest)}")',
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_blockcopy_modifier_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    modifier = instr.source
    if not isinstance(modifier, CopyModifier):
        raise TypeError("blockcopy modifier compiler requires CopyModifier source")

    stem = ctx.next_name("blockcopymod")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        modifier.source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    mode = modifier.mode
    dst_type = _range_type_name(instr.dest)
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    enabled_body: list[str] = [
        *src_setup,
        *dst_setup,
        f"if len({src_indices}) != len({dst_indices}):",
        f'    raise ValueError(f"BlockCopy length mismatch: source has {{len({src_indices})}} elements, dest has {{len({dst_indices})}} elements")',
    ]

    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                "    _converted = []",
                f"    for _src_idx in {src_indices}:",
                f"        _raw_char = _text_from_source_value({src_symbol}[_src_idx])",
                "        if len(_raw_char) != 1:",
                '            raise ValueError("BlockCopy text->numeric conversion requires single CHAR values")',
                f'        _numeric = _store_numeric_text_digit(_raw_char, "{mode}")',
                f'        _converted.append(_store_copy_value_to_type(_numeric, "{dst_type}"))',
                f"    for _dst_idx, _converted_value in zip({dst_indices}, _converted):",
                f"        {dst_symbol}[_dst_idx] = _converted_value",
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "text":
        ctx.mark_helper("_render_text_from_numeric")
        ctx.mark_helper("_termination_char")
        ctx.mark_helper("_store_copy_value_to_type")
        source_type = _optional_range_type_name(modifier.source)
        enabled_body.extend(
            [
                "try:",
                "    _rendered_parts = []",
                f"    for _src_idx in {src_indices}:",
                "        _rendered_parts.append(",
                "            _render_text_from_numeric(",
                f"                {src_symbol}[_src_idx],",
                f"                source_type={source_type!r},",
                f"                suppress_zero={modifier.suppress_zero!r},",
                f"                pad={modifier.pad!r},",
                f"                exponential={modifier.exponential!r},",
                "            )",
                "        )",
                "    _rendered = ''.join(_rendered_parts)",
                f"    _rendered += _termination_char({modifier.termination_code!r})",
                f"    if len(_rendered) != len({dst_indices}):",
                '        raise ValueError("formatted text length does not match destination range")',
                f"    for _dst_idx, _char in zip({dst_indices}, _rendered):",
                f'        {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_char, "{dst_type}")',
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        ctx.mark_helper("_store_copy_value_to_type")
        enabled_body.extend(
            [
                "try:",
                "    _converted = []",
                f"    for _src_idx in {src_indices}:",
                f"        _char = _ascii_char_from_code(int({src_symbol}[_src_idx]) & 0xFF)",
                f'        _converted.append(_store_copy_value_to_type(_char, "{dst_type}"))',
                f"    for _dst_idx, _converted_value in zip({dst_indices}, _converted):",
                f"        {dst_symbol}[_dst_idx] = _converted_value",
                "except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    else:
        enabled_body.extend(fault_body)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_fill_instruction(
    instr: FillInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr.value, CopyModifier):
        return _compile_fill_modifier_instruction(instr, enabled_expr, ctx, indent)
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("fill")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    value_expr = _compile_value(instr.value, ctx)
    enabled_body = [
        *dst_setup,
        f"_fill_value = {value_expr}",
        f"for _dst_idx in {dst_indices}:",
        f'    {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_fill_value, "{_range_type_name(instr.dest)}")',
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_fill_modifier_instruction(
    instr: FillInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    modifier = instr.value
    if not isinstance(modifier, CopyModifier):
        raise TypeError("fill modifier compiler requires CopyModifier value")

    stem = ctx.next_name("fillmod")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    mode = modifier.mode
    dst_type = _range_type_name(instr.dest)
    source_expr = _compile_value(modifier.source, ctx)
    enabled_body: list[str] = [
        *dst_setup,
        f"if not {dst_indices}:",
        "    pass",
        "else:",
    ]

    inner: list[str] = []
    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        ctx.mark_helper("_store_copy_value_to_type")
        inner.extend(
            [
                f"_fill_text = _text_from_source_value({source_expr})",
                "if len(_fill_text) != 1:",
                '    raise ValueError("fill text->numeric conversion requires a single source character")',
                f'_fill_numeric = _store_numeric_text_digit(_fill_text, "{mode}")',
                f'_fill_value = _store_copy_value_to_type(_fill_numeric, "{dst_type}")',
                f"for _dst_idx in {dst_indices}:",
                f"    {dst_symbol}[_dst_idx] = _fill_value",
            ]
        )
    elif mode == "text":
        ctx.mark_helper("_render_text_from_numeric")
        ctx.mark_helper("_termination_char")
        ctx.mark_helper("_store_copy_value_to_type")
        source_type = _optional_value_type_name(modifier.source)
        inner.extend(
            [
                f'if "{dst_type}" != "CHAR":',
                '    raise TypeError("fill(as_text(...)) requires CHAR destination range")',
                "_fill_text = _render_text_from_numeric(",
                f"    {source_expr},",
                f"    source_type={source_type!r},",
                f"    suppress_zero={modifier.suppress_zero!r},",
                f"    pad={modifier.pad!r},",
                f"    exponential={modifier.exponential!r},",
                ")",
                f"_fill_text += _termination_char({modifier.termination_code!r})",
                f"if len(_fill_text) > len({dst_indices}):",
                '    raise ValueError("formatted fill text exceeds destination range")',
                f"for _fill_offset, _dst_idx in enumerate({dst_indices}):",
                "    if _fill_offset < len(_fill_text):",
                f'        {dst_symbol}[_dst_idx] = _store_copy_value_to_type(_fill_text[_fill_offset], "CHAR")',
                "    else:",
                f"        {dst_symbol}[_dst_idx] = ''",
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        ctx.mark_helper("_store_copy_value_to_type")
        inner.extend(
            [
                f"_fill_char = _ascii_char_from_code(int({source_expr}) & 0xFF)",
                f'_fill_value = _store_copy_value_to_type(_fill_char, "{dst_type}")',
                f"for _dst_idx in {dst_indices}:",
                f"    {dst_symbol}[_dst_idx] = _fill_value",
            ]
        )
    else:
        inner.append(f'raise ValueError("Unsupported fill modifier mode: {mode}")')

    enabled_body.extend(_indent_body(inner, 4))
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_search_instruction(
    instr: SearchInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    stem = ctx.next_name("search")
    range_setup, range_symbol, range_indices, range_addrs = _compile_range_setup(
        instr.search_range,
        ctx,
        stem=f"{stem}_rng",
        include_addresses=True,
    )
    result_symbol = ctx.symbol_for_tag(instr.result)
    found_symbol = ctx.symbol_for_tag(instr.found)
    value_expr = _compile_value(instr.value, ctx)
    range_type = _range_type_name(instr.search_range)
    compare_expr = _search_compare_expr(instr.condition, "_candidate", "_rhs")
    static_len = _static_range_length(instr.search_range)
    miss_body = [f"{result_symbol} = -1", f"{found_symbol} = False"]

    text_path = range_type == "CHAR"
    if text_path and instr.condition not in {"==", "!="}:
        raise ValueError("Text search only supports '==' and '!=' conditions")

    cursor_body: list[str] = []
    if instr.continuous:
        cursor_body.extend(
            [
                f"_current_result = int({result_symbol})",
                "if _current_result == 0:",
                "    _cursor_index = 0",
                "elif _current_result == -1:",
                "    _cursor_index = None",
                "else:",
            ]
        )
        if _range_reverse(instr.search_range):
            cursor_body.extend(
                [
                    "    _cursor_index = None",
                    f"    for _idx, _addr in enumerate({range_addrs}):",
                    "        if _addr < _current_result:",
                    "            _cursor_index = _idx",
                    "            break",
                ]
            )
        else:
            cursor_body.extend(
                [
                    "    _cursor_index = None",
                    f"    for _idx, _addr in enumerate({range_addrs}):",
                    "        if _addr > _current_result:",
                    "            _cursor_index = _idx",
                    "            break",
                ]
            )
    else:
        cursor_body.append("_cursor_index = 0")

    enabled_body: list[str] = [*range_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if not {range_addrs}:",
                "    _cursor_index = None",
                "else:",
                *_indent_body(cursor_body, 4),
            ]
        )
    elif static_len == 0:
        enabled_body.append("_cursor_index = None")
    else:
        enabled_body.extend(cursor_body)

    enabled_body.extend(
        [
            "if _cursor_index is None:",
            *[f"    {line}" for line in miss_body],
            "else:",
        ]
    )

    if text_path:
        if isinstance(instr.value, str) and instr.value == "":
            raise ValueError("Text search value cannot be empty")
        fixed_window_len = len(instr.value) if isinstance(instr.value, str) else None
        len_expr = str(static_len) if static_len is not None else f"len({range_indices})"
        match_lines = [
            "if _cursor_index > _last_start:",
            *[f"    {line}" for line in miss_body],
            "else:",
            "    _matched = None",
            "    for _start in range(_cursor_index, _last_start + 1):",
            "        _candidate = ''.join(str("
            f"{range_symbol}[{range_indices}[_start + _off]]) for _off in range(_window_len))",
            f"        if ({'(_candidate == _rhs)' if instr.condition == '==' else '(_candidate != _rhs)'}):",
            "            _matched = _start",
            "            break",
            "    if _matched is None:",
            *[f"        {line}" for line in miss_body],
            "    else:",
            f"        {result_symbol} = {range_addrs}[_matched]",
            f"        {found_symbol} = True",
        ]
        enabled_body.extend(
            [
                f"    _rhs = {value_expr}"
                if isinstance(instr.value, str)
                else f"    _rhs = str({value_expr})",
            ]
        )
        if fixed_window_len is None:
            enabled_body.extend(
                [
                    '    if _rhs == "":',
                    '        raise ValueError("Text search value cannot be empty")',
                    "    _window_len = len(_rhs)",
                    f"    if _window_len > {len_expr}:",
                    *[f"        {line}" for line in miss_body],
                    "    else:",
                    f"        _last_start = {len_expr} - _window_len",
                    *_indent_body(match_lines, 8),
                ]
            )
        elif static_len is not None and fixed_window_len > static_len:
            enabled_body.extend([f"    {line}" for line in miss_body])
        else:
            enabled_body.extend(
                [
                    f"    _window_len = {fixed_window_len}",
                    f"    _last_start = {len_expr} - _window_len",
                    *_indent_body(match_lines, 4),
                ]
            )
    else:
        len_expr = str(static_len) if static_len is not None else f"len({range_indices})"
        enabled_body.extend(
            [
                f"    _rhs = {value_expr}",
                "    _matched = None",
                f"    for _idx in range(_cursor_index, {len_expr}):",
                f"        _candidate = {range_symbol}[{range_indices}[_idx]]",
                f"        if {compare_expr}:",
                "            _matched = _idx",
                "            break",
                "    if _matched is None:",
                *[f"        {line}" for line in miss_body],
                "    else:",
                f"        {result_symbol} = {range_addrs}[_matched]",
                f"        {found_symbol} = True",
            ]
        )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_shift_instruction(
    instr: ShiftInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if _range_type_name(instr.bit_range) != "BOOL":
        raise TypeError("shift bit_range must contain BOOL tags")
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    key = f"_shift_prev_clock:{ctx.state_key_for(instr)}"
    stem = ctx.next_name("shift")
    range_setup, range_symbol, range_indices, _ = _compile_range_setup(
        instr.bit_range, ctx, stem=f"{stem}_rng", include_addresses=False
    )
    static_len = _static_range_length(instr.bit_range)
    if static_len == 0:
        raise ValueError("shift bit_range resolved to an empty range")
    shift_len_expr = str(static_len) if static_len is not None else f"len({range_indices})"
    clock_expr = compile_condition(instr.clock_condition, ctx)
    reset_expr = compile_condition(instr.reset_condition, ctx)
    lines = [
        *range_setup,
        f"_clock_curr = {clock_expr}",
        f"_clock_prev = bool(_mem.get({key!r}, False))",
        "_rising_edge = _clock_curr and not _clock_prev",
        "if _rising_edge:",
        f"    _prev_values = [bool({range_symbol}[_idx]) for _idx in {range_indices}]",
        f"    {range_symbol}[{range_indices}[0]] = bool({enabled_expr})",
        f"    for _pos in range(1, {shift_len_expr}):",
        f"        {range_symbol}[{range_indices}[_pos]] = _prev_values[_pos - 1]",
        f"if {reset_expr}:",
        f"    for _idx in {range_indices}:",
        f"        {range_symbol}[_idx] = False",
        f"_mem[{key!r}] = _clock_curr",
    ]
    return [" " * indent + line for line in lines]


def _compile_step_selector_lines(
    *,
    step_var: str,
    target_var: str,
    value_exprs: list[str],
) -> list[str]:
    lines: list[str] = []
    for idx, expr in enumerate(value_exprs, start=1):
        branch = "if" if idx == 1 else "elif"
        lines.append(f"{branch} {step_var} == {idx}:")
        lines.append(f"    {target_var} = {expr}")
    return lines


def _compile_event_drum_instruction(
    instr: EventDrumInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")

    step_symbol = ctx.symbol_for_tag(instr.current_step)
    completion_symbol = ctx.symbol_for_tag(instr.completion_flag)
    output_symbols = [ctx.symbol_for_tag(tag) for tag in instr.outputs]
    pattern_literal = repr(tuple(tuple(bool(cell) for cell in row) for row in instr.pattern))
    step_count = len(instr.pattern)
    key_base = ctx.state_key_for(instr)
    event_prev_key = f"_drum_event_prev:{key_base}"
    event_ready_key = f"_drum_event_ready:{key_base}"
    last_step_key = f"_drum_last_step:{key_base}"
    jump_prev_key = f"_drum_jump_prev:{key_base}"
    jog_prev_key = f"_drum_jog_prev:{key_base}"

    event_exprs = [f"bool({compile_condition(cond, ctx)})" for cond in instr.events]
    reset_expr = compile_condition(instr.reset_condition, ctx)

    lines: list[str] = [
        f"_enabled = bool({enabled_expr})",
        f"_step_raw = int({step_symbol})",
        "_step = _step_raw",
        "_step_changed = False",
        f"if _enabled and ((_step < 1) or (_step > {step_count})):",
        "    _step = 1",
        f"    {step_symbol} = 1",
        "    _step_changed = True",
        f"elif (_step < 1) or (_step > {step_count}):",
        "    _step = 1",
    ]

    if instr.jump_condition is not None:
        jump_expr = compile_condition(instr.jump_condition, ctx)
        lines.extend(
            [
                f"_jump_curr = bool({jump_expr})",
                f"_jump_prev = bool(_mem.get({jump_prev_key!r}, False))",
                "_jump_edge = _jump_curr and (not _jump_prev)",
            ]
        )
    else:
        lines.extend(["_jump_curr = False", "_jump_edge = False"])

    if instr.jog_condition is not None:
        jog_expr = compile_condition(instr.jog_condition, ctx)
        lines.extend(
            [
                f"_jog_curr = bool({jog_expr})",
                f"_jog_prev = bool(_mem.get({jog_prev_key!r}, False))",
                "_jog_edge = _jog_curr and (not _jog_prev)",
            ]
        )
    else:
        lines.extend(["_jog_curr = False", "_jog_edge = False"])

    lines.append(f"_reset_active = bool({reset_expr})")
    lines.append("if _enabled:")
    lines.extend(
        _indent_body(
            [
                *_compile_step_selector_lines(
                    step_var="_step",
                    target_var="_event_curr",
                    value_exprs=event_exprs,
                ),
                f"_last_step = int(_mem.get({last_step_key!r}, 0))",
                f"_event_ready = bool(_mem.get({event_ready_key!r}, True))",
                f"_event_prev = bool(_mem.get({event_prev_key!r}, False))",
                "if (_last_step != _step) or _step_changed:",
                "    _event_ready = (not _event_curr)",
                "    _event_prev = _event_curr",
                "elif (not _event_ready) and (not _event_curr):",
                "    _event_ready = True",
                "if _event_ready and _event_curr and (not _event_prev):",
                f"    if _step < {step_count}:",
                "        _step += 1",
                f"        {step_symbol} = _step",
                "        _step_changed = True",
                "    else:",
                f"        {completion_symbol} = True",
            ],
            4,
        )
    )

    lines.extend(
        [
            "if _reset_active:",
            "    _step = 1",
            "    _step_changed = True",
            f"    {step_symbol} = 1",
            f"    {completion_symbol} = False",
        ]
    )

    if instr.jump_condition is not None and instr.jump_step is not None:
        jump_step_expr = _compile_value(instr.jump_step, ctx)
        lines.extend(
            [
                "if _enabled and _jump_edge:",
                f"    _target = int({jump_step_expr})",
                f"    if 1 <= _target <= {step_count}:",
                "        _step_changed = _step_changed or (_step != _target)",
                "        _step = _target",
                f"        {step_symbol} = _step",
            ]
        )

    if instr.jog_condition is not None:
        lines.extend(
            [
                f"if _enabled and _jog_edge and (_step < {step_count}):",
                "    _step += 1",
                "    _step_changed = True",
                f"    {step_symbol} = _step",
            ]
        )

    lines.extend(
        [
            "if _enabled or _reset_active:",
            f"    _row = {pattern_literal}[_step - 1]",
        ]
    )
    for idx, output_symbol in enumerate(output_symbols):
        lines.append(f"    {output_symbol} = bool(_row[{idx}])")

    lines.extend(
        [
            *_compile_step_selector_lines(
                step_var="_step",
                target_var="_event_curr_final",
                value_exprs=event_exprs,
            ),
            f"_event_ready_final = bool(_mem.get({event_ready_key!r}, True))",
            "if _step_changed:",
            "    _event_ready_final = (not _event_curr_final)",
            "elif (not _event_ready_final) and (not _event_curr_final):",
            "    _event_ready_final = True",
            f"_mem[{event_ready_key!r}] = _event_ready_final",
            f"_mem[{event_prev_key!r}] = _event_curr_final",
            f"_mem[{last_step_key!r}] = _step",
        ]
    )
    if instr.jump_condition is not None:
        lines.append(f"_mem[{jump_prev_key!r}] = _jump_curr")
    if instr.jog_condition is not None:
        lines.append(f"_mem[{jog_prev_key!r}] = _jog_curr")

    return [" " * indent + line for line in lines]


def _compile_time_drum_instruction(
    instr: TimeDrumInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")

    step_symbol = ctx.symbol_for_tag(instr.current_step)
    completion_symbol = ctx.symbol_for_tag(instr.completion_flag)
    accumulator_symbol = ctx.symbol_for_tag(instr.accumulator)
    output_symbols = [ctx.symbol_for_tag(tag) for tag in instr.outputs]
    pattern_literal = repr(tuple(tuple(bool(cell) for cell in row) for row in instr.pattern))
    step_count = len(instr.pattern)
    key_base = ctx.state_key_for(instr)
    frac_key = f"_drum_time_frac:{key_base}"
    jump_prev_key = f"_drum_jump_prev:{key_base}"
    jog_prev_key = f"_drum_jog_prev:{key_base}"
    max_acc = _INT_MAX if instr.accumulator.type == TagType.INT else _DINT_MAX

    preset_exprs = [f"int({_compile_value(preset, ctx)})" for preset in instr.presets]
    reset_expr = compile_condition(instr.reset_condition, ctx)
    unit_expr = _timer_dt_to_units_expr(instr.unit, "_dt", "_frac")

    lines: list[str] = [
        f"_enabled = bool({enabled_expr})",
        f"_step_raw = int({step_symbol})",
        "_step = _step_raw",
        "_step_changed = False",
        "_reset_step_data = False",
        f"if _enabled and ((_step < 1) or (_step > {step_count})):",
        "    _step = 1",
        "    _step_changed = True",
        "    _reset_step_data = True",
        f"    {step_symbol} = 1",
        f"elif (_step < 1) or (_step > {step_count}):",
        "    _step = 1",
        f"_acc = int({accumulator_symbol})",
        f"_frac = float(_mem.get({frac_key!r}, 0.0))",
    ]

    if instr.jump_condition is not None:
        jump_expr = compile_condition(instr.jump_condition, ctx)
        lines.extend(
            [
                f"_jump_curr = bool({jump_expr})",
                f"_jump_prev = bool(_mem.get({jump_prev_key!r}, False))",
                "_jump_edge = _jump_curr and (not _jump_prev)",
            ]
        )
    else:
        lines.extend(["_jump_curr = False", "_jump_edge = False"])

    if instr.jog_condition is not None:
        jog_expr = compile_condition(instr.jog_condition, ctx)
        lines.extend(
            [
                f"_jog_curr = bool({jog_expr})",
                f"_jog_prev = bool(_mem.get({jog_prev_key!r}, False))",
                "_jog_edge = _jog_curr and (not _jog_prev)",
            ]
        )
    else:
        lines.extend(["_jog_curr = False", "_jog_edge = False"])

    lines.append(f"_reset_active = bool({reset_expr})")
    lines.append("if _enabled:")
    lines.extend(
        _indent_body(
            [
                "_dt = float(_mem.get('_dt', 0.0))",
                f"_dt_units = {unit_expr}",
                "_int_units = int(_dt_units)",
                "_frac = _dt_units - _int_units",
                f"_acc = min(_acc + _int_units, {max_acc})",
                *_compile_step_selector_lines(
                    step_var="_step",
                    target_var="_preset",
                    value_exprs=preset_exprs,
                ),
                "if _acc >= _preset:",
                f"    if _step < {step_count}:",
                "        _step += 1",
                "        _step_changed = True",
                "        _reset_step_data = True",
                f"        {step_symbol} = _step",
                "    else:",
                f"        {completion_symbol} = True",
            ],
            4,
        )
    )

    lines.extend(
        [
            "if _reset_active:",
            "    _step = 1",
            "    _step_changed = True",
            "    _reset_step_data = True",
            f"    {step_symbol} = 1",
            f"    {completion_symbol} = False",
        ]
    )

    if instr.jump_condition is not None and instr.jump_step is not None:
        jump_step_expr = _compile_value(instr.jump_step, ctx)
        lines.extend(
            [
                "if _enabled and _jump_edge:",
                f"    _target = int({jump_step_expr})",
                f"    if 1 <= _target <= {step_count}:",
                "        _step_changed = _step_changed or (_step != _target)",
                "        _step = _target",
                "        _reset_step_data = True",
                f"        {step_symbol} = _step",
            ]
        )

    if instr.jog_condition is not None:
        lines.extend(
            [
                f"if _enabled and _jog_edge and (_step < {step_count}):",
                "    _step += 1",
                "    _step_changed = True",
                "    _reset_step_data = True",
                f"    {step_symbol} = _step",
            ]
        )

    lines.extend(
        [
            "if _reset_step_data:",
            "    _acc = 0",
            "    _frac = 0.0",
            "if _enabled or _reset_active:",
            f"    _row = {pattern_literal}[_step - 1]",
        ]
    )
    for idx, output_symbol in enumerate(output_symbols):
        lines.append(f"    {output_symbol} = bool(_row[{idx}])")

    lines.extend(
        [
            "if _enabled or _reset_active or _step_changed or _reset_step_data:",
            f"    {accumulator_symbol} = _acc",
            f"    _mem[{frac_key!r}] = _frac",
        ]
    )

    if instr.jump_condition is not None:
        lines.append(f"_mem[{jump_prev_key!r}] = _jump_curr")
    if instr.jog_condition is not None:
        lines.append(f"_mem[{jog_prev_key!r}] = _jog_curr")

    return [" " * indent + line for line in lines]


def _compile_pack_bits_instruction(
    instr: PackBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"INT", "WORD", "DINT", "REAL"}:
        raise TypeError("pack_bits destination must be INT, WORD, DINT, or REAL")
    if _range_type_name(instr.bit_block) != "BOOL":
        raise TypeError("pack_bits source range must contain BOOL tags")
    width = 16 if dest_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("packbits")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.bit_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    static_len = _static_range_length(instr.bit_block)
    if static_len is not None and static_len > width:
        raise ValueError(
            f"pack_bits destination width is {width} bits but block has {static_len} tags"
        )
    enabled_body = [*src_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({src_indices}) > {width}:",
                f'    raise ValueError(f"pack_bits destination width is {width} bits but block has {{len({src_indices})}} tags")',
            ]
        )
    enabled_body.extend(
        [
            "_packed = 0",
            f"for _bit_index, _src_idx in enumerate({src_indices}):",
            f"    if bool({src_symbol}[_src_idx]):",
            "        _packed |= (1 << _bit_index)",
            f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
            *_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_pack_words_instruction(
    instr: PackWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"DINT", "REAL"}:
        raise TypeError("pack_words destination must be DINT or REAL")
    if _range_type_name(instr.word_block) not in {"INT", "WORD"}:
        raise TypeError("pack_words source range must contain INT/WORD tags")
    stem = ctx.next_name("packwords")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.word_block, ctx, stem=f"{stem}_src", include_addresses=False
    )
    static_len = _static_range_length(instr.word_block)
    if static_len is not None and static_len != 2:
        raise ValueError(f"pack_words requires exactly 2 source tags; got {static_len}")
    enabled_body = [*src_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({src_indices}) != 2:",
                f'    raise ValueError(f"pack_words requires exactly 2 source tags; got {{len({src_indices})}}")',
            ]
        )
    enabled_body.extend(
        [
            f"_lo_value = int({src_symbol}[{src_indices}[0]])",
            f"_hi_value = int({src_symbol}[{src_indices}[1]])",
            "_packed = ((_hi_value << 16) | (_lo_value & 0xFFFF))",
            f"_packed_value = {_pack_store_expr('_packed', dest_type, ctx)}",
            *_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_pack_text_instruction(
    instr: PackTextInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _range_type_name(instr.source_range)
    if source_type != "CHAR":
        raise TypeError("pack_text source range must contain CHAR tags")
    dest_type = _value_type_name(instr.dest)
    if dest_type not in {"INT", "DINT", "WORD", "REAL"}:
        raise TypeError("pack_text destination must be INT, DINT, WORD, or REAL")
    ctx.mark_helper("_parse_pack_text_value")
    ctx.mark_helper("_store_copy_value_to_type")
    stem = ctx.next_name("packtext")
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.source_range, ctx, stem=f"{stem}_src", include_addresses=False
    )
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    enabled_body = [
        *src_setup,
        f"_text = ''.join(str({src_symbol}[_idx]) for _idx in {src_indices})",
    ]
    if instr.allow_whitespace:
        enabled_body.append("_text = _text.strip()")
    else:
        enabled_body.extend(
            [
                "if _text != _text.strip():",
                *_indent_body(fault_body, 4),
                "else:",
                "    try:",
                f'        _parsed = _parse_pack_text_value(_text, "{dest_type}")',
                f'        _packed_value = _store_copy_value_to_type(_parsed, "{dest_type}")',
                *_indent_body(
                    _compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0), 8
                ),
                "    except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 8),
            ]
        )
        return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
    enabled_body.extend(
        [
            "try:",
            f'    _parsed = _parse_pack_text_value(_text, "{dest_type}")',
            f'    _packed_value = _store_copy_value_to_type(_parsed, "{dest_type}")',
            *_indent_body(_compile_assignment_lines(instr.dest, "_packed_value", ctx, indent=0), 4),
            "except (TypeError, ValueError, OverflowError):",
            *_indent_body(fault_body, 4),
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_set_out_of_range_fault_body(ctx: CodegenContext) -> list[str]:
    fault_symbol = ctx.symbol_if_referenced(_FAULT_OUT_OF_RANGE_TAG)
    if fault_symbol is None:
        return ["pass"]
    return [f"{fault_symbol} = True"]


def _compile_unpack_bits_instruction(
    instr: UnpackToBitsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _value_type_name(instr.source)
    if source_type not in {"INT", "WORD", "DINT", "REAL"}:
        raise TypeError("unpack_to_bits source must be INT, WORD, DINT, or REAL")
    if _range_type_name(instr.bit_block) != "BOOL":
        raise TypeError("unpack_to_bits destination range must contain BOOL tags")
    width = 16 if source_type in {"INT", "WORD"} else 32
    stem = ctx.next_name("unpackbits")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.bit_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(instr.source, ctx)})"
    elif source_type in {"INT", "WORD"}:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFF)"
    else:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFFFFFF)"
    static_len = _static_range_length(instr.bit_block)
    if static_len is not None and static_len > width:
        raise ValueError(
            f"unpack_to_bits source width is {width} bits but block has {static_len} tags"
        )
    enabled_body = [*dst_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({dst_indices}) > {width}:",
                f'    raise ValueError(f"unpack_to_bits source width is {width} bits but block has {{len({dst_indices})}} tags")',
            ]
        )
    enabled_body.extend(
        [
            f"_bits = {bits_expr}",
            f"for _bit_index, _dst_idx in enumerate({dst_indices}):",
            f"    {dst_symbol}[_dst_idx] = bool((_bits >> _bit_index) & 1)",
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_unpack_words_instruction(
    instr: UnpackToWordsInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source_type = _value_type_name(instr.source)
    if source_type not in {"DINT", "REAL"}:
        raise TypeError("unpack_to_words source must be DINT or REAL")
    dst_type = _range_type_name(instr.word_block)
    if dst_type not in {"INT", "WORD"}:
        raise TypeError("unpack_to_words destination range must contain INT/WORD tags")
    stem = ctx.next_name("unpackwords")
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.word_block, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    if source_type == "REAL":
        ctx.mark_helper("_float_to_int_bits")
        bits_expr = f"_float_to_int_bits({_compile_value(instr.source, ctx)})"
    else:
        bits_expr = f"(int({_compile_value(instr.source, ctx)}) & 0xFFFFFFFF)"
    lo_expr = "_lo_word"
    hi_expr = "_hi_word"
    if dst_type == "INT":
        ctx.mark_helper("_wrap_int")
        lo_store = f"_wrap_int({lo_expr}, 16, True)"
        hi_store = f"_wrap_int({hi_expr}, 16, True)"
    else:
        lo_store = f"({lo_expr} & 0xFFFF)"
        hi_store = f"({hi_expr} & 0xFFFF)"
    static_len = _static_range_length(instr.word_block)
    if static_len is not None and static_len != 2:
        raise ValueError(f"unpack_to_words requires exactly 2 destination tags; got {static_len}")
    enabled_body = [*dst_setup]
    if static_len is None:
        enabled_body.extend(
            [
                f"if len({dst_indices}) != 2:",
                f'    raise ValueError(f"unpack_to_words requires exactly 2 destination tags; got {{len({dst_indices})}}")',
            ]
        )
    enabled_body.extend(
        [
            f"_bits = {bits_expr}",
            "_lo_word = (_bits & 0xFFFF)",
            "_hi_word = ((_bits >> 16) & 0xFFFF)",
            f"{dst_symbol}[{dst_indices}[0]] = {lo_store}",
            f"{dst_symbol}[{dst_indices}[1]] = {hi_store}",
        ]
    )
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_function_call_instruction(
    instr: FunctionCallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    fn_symbol = ctx.register_function_source(instr._fn)
    fn_name = getattr(instr._fn, "__name__", type(instr._fn).__name__)
    result_var = f"_{ctx.next_name('fn_result')}"
    kwargs = ", ".join(
        f"{name}={_compile_value(value, ctx)}" for name, value in sorted(instr._ins.items())
    )
    call_expr = f"{fn_symbol}({kwargs})" if kwargs else f"{fn_symbol}()"
    enabled_body = [f"{result_var} = {call_expr}"]
    if instr._outs:
        enabled_body.extend(
            [
                f"if {result_var} is None:",
                f'    raise TypeError("run_function: {fn_name!r} returned None but outs were declared")',
            ]
        )
        ctx.mark_helper("_store_copy_value_to_type")
        for key, target in sorted(instr._outs.items()):
            target_type = _value_type_name(target)
            enabled_body.extend(
                [
                    f"if {key!r} not in {result_var}:",
                    "    raise KeyError(",
                    f'        f"run_function: {fn_name!r} missing key {key!r}; got {{sorted({result_var})}}"',
                    "    )",
                ]
            )
            value_expr = f'_store_copy_value_to_type({result_var}[{key!r}], "{target_type}")'
            enabled_body.extend(_compile_assignment_lines(target, value_expr, ctx, indent=0))
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_enabled_function_call_instruction(
    instr: EnabledFunctionCallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    fn_symbol = ctx.register_function_source(instr._fn)
    result_var = f"_{ctx.next_name('fn_result')}"
    kwargs = ", ".join(
        f"{name}={_compile_value(value, ctx)}" for name, value in sorted(instr._ins.items())
    )
    call_args = enabled_expr
    if kwargs:
        call_args += f", {kwargs}"
    lines = [f"{' ' * indent}{result_var} = {fn_symbol}({call_args})"]
    if instr._outs:
        fn_name = getattr(instr._fn, "__name__", type(instr._fn).__name__)
        ctx.mark_helper("_store_copy_value_to_type")
        lines.extend(
            [
                f"{' ' * indent}if {result_var} is None:",
                f'{" " * (indent + 4)}raise TypeError("run_enabled_function: {fn_name!r} returned None but outs were declared")',
            ]
        )
        for key, target in sorted(instr._outs.items()):
            target_type = _value_type_name(target)
            lines.extend(
                [
                    f"{' ' * indent}if {key!r} not in {result_var}:",
                    f"{' ' * (indent + 4)}raise KeyError(",
                    f'{" " * (indent + 8)}f"run_enabled_function: {fn_name!r} missing key {key!r}; got {{sorted({result_var})}}"',
                    f"{' ' * (indent + 4)})",
                ]
            )
            value_expr = f'_store_copy_value_to_type({result_var}[{key!r}], "{target_type}")'
            lines.extend(_compile_assignment_lines(target, value_expr, ctx, indent=indent))
    return lines


def _compile_for_loop_instruction(
    instr: ForLoopInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    count_expr = _compile_value(instr.count, ctx)
    idx_symbol = ctx.symbol_for_tag(instr.idx_tag)
    disabled_children = _compile_instruction_list(instr.instructions, "False", ctx, indent=0)
    enabled_children = _compile_instruction_list(instr.instructions, "True", ctx, indent=0)
    body = [
        f"_iterations = max(0, int({count_expr}))",
        "for _for_i in range(_iterations):",
        f"    {idx_symbol} = _for_i",
        *_indent_body(enabled_children, 4),
    ]
    return _compile_guarded_instruction(
        instr,
        enabled_expr,
        ctx,
        indent,
        body,
        disabled_body=disabled_children,
    )


def _compile_instruction_list(
    instructions: list[Any],
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    lines: list[str] = []
    for instruction in instructions:
        lines.extend(compile_instruction(instruction, enabled_expr, ctx, indent))
    return lines


def _copy_modifier_target_info(
    target: Tag | IndirectRef | IndirectExprRef,
    ctx: CodegenContext,
    stem: str,
) -> tuple[list[str], str, str, str | None, str]:
    if isinstance(target, Tag):
        block_info = ctx.tag_block_addresses.get(target.name)
        if block_info is None:
            return [], "scalar", ctx.symbol_for_tag(target), None, target.type.name
        block_id, addr = block_info
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for tag-backed target {target.name!r}")
        symbol = ctx.symbol_for_block(binding.block)
        start_var = f"_{stem}_start_idx"
        return (
            [f"{start_var} = {addr - binding.start}"],
            "block",
            symbol,
            start_var,
            target.type.name,
        )

    if isinstance(target, IndirectRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        ptr = _compile_value(target.pointer, ctx)
        return (
            [f"{start_var} = {helper}(int({ptr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    if isinstance(target, IndirectExprRef):
        binding = ctx.block_bindings.get(id(target.block))
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        start_var = f"_{stem}_start_idx"
        expr = compile_expression(target.expr, ctx)
        return (
            [f"{start_var} = {helper}(int({expr}))"],
            "block",
            symbol,
            start_var,
            binding.tag_type.name,
        )

    raise TypeError(f"Unsupported copy modifier target type: {type(target).__name__}")


def _copy_modifier_write_lines(
    *,
    values_var: str,
    target_kind: str,
    target_symbol: str,
    target_start_var: str | None,
    fault_body: list[str],
) -> list[str]:
    if target_kind == "scalar":
        return [
            f"if len({values_var}) > 1:",
            *_indent_body(fault_body, 4),
            f"elif len({values_var}) == 1:",
            f"    {target_symbol} = {values_var}[0]",
        ]

    if target_start_var is None:
        raise RuntimeError("copy modifier block target is missing start index")
    return [
        f"_copy_count = len({values_var})",
        "if _copy_count == 0:",
        "    pass",
        f"elif ({target_start_var} < 0) or (({target_start_var} + _copy_count) > len({target_symbol})):",
        *_indent_body(fault_body, 4),
        "else:",
        f"    for _copy_offset, _copy_value in enumerate({values_var}):",
        f"        {target_symbol}[{target_start_var} + _copy_offset] = _copy_value",
    ]


def _compile_assignment_lines(
    target: Tag | IndirectRef | IndirectExprRef,
    value_expr: str,
    ctx: CodegenContext,
    *,
    indent: int,
) -> list[str]:
    lvalue = _compile_lvalue(target, ctx)
    return [f"{' ' * indent}{lvalue} = {value_expr}"]


def _compile_lvalue(target: Tag | IndirectRef | IndirectExprRef, ctx: CodegenContext) -> str:
    if isinstance(target, Tag):
        return ctx.symbol_for_tag(target)
    if isinstance(target, IndirectRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(f"Missing block binding for indirect target {target.block.name!r}")
        block_symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        ptr = _compile_value(target.pointer, ctx)
        return f"{block_symbol}[{helper}(int({ptr}))]"
    if isinstance(target, IndirectExprRef):
        block_id = id(target.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression target {target.block.name!r}"
            )
        block_symbol = ctx.symbol_for_block(target.block)
        helper = ctx.use_indirect_block(binding.block_id)
        expr = compile_expression(target.expr, ctx)
        return f"{block_symbol}[{helper}(int({expr}))]"
    raise TypeError(f"Unsupported assignment target: {type(target).__name__}")


def _compile_range_setup(
    range_value: Any,
    ctx: CodegenContext,
    *,
    stem: str,
    include_addresses: bool,
) -> tuple[list[str], str, str, str]:
    if not isinstance(range_value, (BlockRange, IndirectBlockRange)):
        raise TypeError(
            f"Expected BlockRange or IndirectBlockRange, got {type(range_value).__name__}"
        )
    binding = ctx.block_bindings[id(range_value.block)]
    symbol = ctx.symbol_for_block(range_value.block)
    if isinstance(range_value, BlockRange):
        name = ctx.next_name(stem)
        indices_var = f"_{name}_indices"
        addrs_var = f"_{name}_addrs"
        addresses = [int(addr) for addr in range_value.addresses]
        indices = [addr - binding.start for addr in addresses]
        indices_expr = _sequence_expr(indices)
        lines = [f"{indices_var} = {indices_expr}"]
        if include_addresses:
            lines.append(f"{addrs_var} = {_sequence_expr(addresses)}")
        else:
            addrs_var = "[]"
        return lines, symbol, indices_var, addrs_var

    helper = ctx.use_indirect_block(binding.block_id)
    name = ctx.next_name(stem)
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_idx"
    indices_var = f"_{name}_indices"
    addrs_var = f"_{name}_addrs"
    start_expr = _compile_address_expr(range_value.start_expr, ctx)
    end_expr = _compile_address_expr(range_value.end_expr, ctx)
    lines = [
        f"{start_var} = int({start_expr})",
        f"{end_var} = int({end_expr})",
        f"if {start_var} > {end_var}:",
        '    raise ValueError("Indirect range start must be <= end")',
        f"{indices_var} = []",
        f"{addrs_var} = []",
        f"for {addr_var} in range({start_var}, {end_var} + 1):",
        f"    {idx_var} = {helper}(int({addr_var}))",
        f"    {indices_var}.append({idx_var})",
        f"    {addrs_var}.append(int({addr_var}))",
    ]
    if range_value.reverse_order:
        lines.extend([f"{indices_var}.reverse()", f"{addrs_var}.reverse()"])
    return lines, symbol, indices_var, addrs_var


def _sequence_expr(values: list[int]) -> str:
    if not values:
        return "[]"
    if len(values) == 1:
        return repr(range(values[0], values[0] + 1))

    step = values[1] - values[0]
    if step != 0 and all(values[i + 1] - values[i] == step for i in range(len(values) - 1)):
        return repr(range(values[0], values[-1] + step, step))
    return repr(values)


def _search_compare_expr(condition: str, left_expr: str, right_expr: str) -> str:
    if condition == "==":
        return f"({left_expr} == {right_expr})"
    if condition == "!=":
        return f"({left_expr} != {right_expr})"
    if condition == "<":
        return f"({left_expr} < {right_expr})"
    if condition == "<=":
        return f"({left_expr} <= {right_expr})"
    if condition == ">":
        return f"({left_expr} > {right_expr})"
    if condition == ">=":
        return f"({left_expr} >= {right_expr})"
    raise ValueError(f"Unsupported search comparison: {condition!r}")


def _pack_store_expr(value_expr: str, dest_type: str, ctx: CodegenContext) -> str:
    if dest_type == "REAL":
        ctx.mark_helper("_int_to_float_bits")
        return f"_int_to_float_bits({value_expr})"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    raise TypeError(f"Unsupported pack destination type: {dest_type}")


def _calc_store_expr(value_expr: str, dest_type: str, mode: str, ctx: CodegenContext) -> str:
    if mode == "hex":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "BOOL":
        return f"bool({value_expr})"
    if dest_type == "REAL":
        return f"float({value_expr})"
    if dest_type == "CHAR":
        return value_expr
    if dest_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if dest_type == "INT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 16, True)"
    if dest_type == "DINT":
        ctx.mark_helper("_wrap_int")
        return f"_wrap_int(int({value_expr}), 32, True)"
    return value_expr


def _timer_dt_to_units_expr(unit: TimeUnit, dt_expr: str, frac_expr: str) -> str:
    if unit == TimeUnit.Tms:
        return f"(({dt_expr} * 1000.0) + {frac_expr})"
    if unit == TimeUnit.Ts:
        return f"(({dt_expr}) + {frac_expr})"
    if unit == TimeUnit.Tm:
        return f"(({dt_expr} / 60.0) + {frac_expr})"
    if unit == TimeUnit.Th:
        return f"(({dt_expr} / 3600.0) + {frac_expr})"
    if unit == TimeUnit.Td:
        return f"(({dt_expr} / 86400.0) + {frac_expr})"
    raise ValueError(f"Unsupported timer unit: {unit}")


def _load_cast_expr(value_expr: str, tag_type: str) -> str:
    if tag_type == "BOOL":
        return f"bool({value_expr})"
    if tag_type == "INT":
        return f"max({_INT_MIN}, min({_INT_MAX}, int({value_expr})))"
    if tag_type == "DINT":
        return f"max({_DINT_MIN}, min({_DINT_MAX}, int({value_expr})))"
    if tag_type == "WORD":
        return f"(int({value_expr}) & 0xFFFF)"
    if tag_type == "REAL":
        return f"float({value_expr})"
    if tag_type == "CHAR":
        return f"({value_expr} if isinstance({value_expr}, str) else '')"
    return value_expr


def _compile_target_write_lines(
    target: Tag | BlockRange | IndirectBlockRange,
    value_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    if isinstance(target, Tag):
        return [f"{sp}{ctx.symbol_for_tag(target)} = {value_expr}"]

    if isinstance(target, BlockRange):
        binding = ctx.block_bindings[id(target.block)]
        symbol = ctx.symbol_for_block(target.block)
        lines: list[str] = []
        for addr in target.addresses:
            index = addr - binding.start
            lines.append(f"{sp}{symbol}[{index}] = {value_expr}")
        return lines or [f"{sp}pass"]

    binding = ctx.block_bindings[id(target.block)]
    symbol = ctx.symbol_for_block(target.block)
    helper = ctx.use_indirect_block(binding.block_id)
    name = ctx.next_name("irange")
    start_var = f"_{name}_start"
    end_var = f"_{name}_end"
    addr_list = f"_{name}_indices"
    addr_var = f"_{name}_addr"
    idx_var = f"_{name}_idx"
    start_expr = _compile_address_expr(target.start_expr, ctx)
    end_expr = _compile_address_expr(target.end_expr, ctx)
    lines = [
        f"{sp}{start_var} = int({start_expr})",
        f"{sp}{end_var} = int({end_expr})",
        f"{sp}if {start_var} > {end_var}:",
        f'{sp}    raise ValueError("Indirect range start must be <= end")',
        f"{sp}{addr_list} = []",
        f"{sp}for {addr_var} in range({start_var}, {end_var} + 1):",
        f"{sp}    {idx_var} = {helper}(int({addr_var}))",
        f"{sp}    {addr_list}.append({idx_var})",
    ]
    if target.reverse_order:
        lines.append(f"{sp}{addr_list}.reverse()")
    lines.extend(
        [
            f"{sp}for {idx_var} in {addr_list}:",
            f"{sp}    {symbol}[{idx_var}] = {value_expr}",
        ]
    )
    return lines


def _compile_address_expr(addr: int | Tag | Any, ctx: CodegenContext) -> str:
    if isinstance(addr, int):
        return repr(addr)
    if isinstance(addr, Tag):
        return ctx.symbol_for_tag(addr)
    if isinstance(addr, Expression):
        return compile_expression(addr, ctx)
    raise TypeError(f"Unsupported indirect address expression type: {type(addr).__name__}")


def _compile_indirect_value(indirect_ref: IndirectRef, ctx: CodegenContext) -> str:
    block_id = id(indirect_ref.block)
    binding = ctx.block_bindings.get(block_id)
    if binding is None:
        raise RuntimeError(f"Missing block binding for indirect ref {indirect_ref.block.name!r}")
    block_symbol = ctx.symbol_for_block(indirect_ref.block)
    helper = ctx.use_indirect_block(binding.block_id)
    ptr = _compile_value(indirect_ref.pointer, ctx)
    return f"{block_symbol}[{helper}(int({ptr}))]"


def _compile_value(value: Any, ctx: CodegenContext) -> str:
    if isinstance(value, Tag):
        return ctx.symbol_for_tag(value)
    if isinstance(value, IndirectRef):
        return _compile_indirect_value(value, ctx)
    if isinstance(value, IndirectExprRef):
        block_id = id(value.block)
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            raise RuntimeError(
                f"Missing block binding for indirect expression ref {value.block.name!r}"
            )
        block_symbol = ctx.symbol_for_block(value.block)
        helper = ctx.use_indirect_block(binding.block_id)
        expr = compile_expression(value.expr, ctx)
        return f"{block_symbol}[{helper}(int({expr}))]"
    if isinstance(value, Expression):
        return compile_expression(value, ctx)
    return repr(value)
