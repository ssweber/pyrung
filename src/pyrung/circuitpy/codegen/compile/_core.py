"""Automatically generated module split."""

from __future__ import annotations

from typing import Any

from pyrung.circuitpy.codegen._util import (
    _indent_body,
    _source_location,
    _value_type_name,
)
from pyrung.circuitpy.codegen.context import (
    CodegenContext,
)
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
from pyrung.core.expression import (
    ExprCompare,
    Expression,
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
from pyrung.core.instruction.send_receive import (
    ModbusReceiveInstruction,
    ModbusSendInstruction,
)
from pyrung.core.rung import Rung as LogicRung
from pyrung.core.tag import ImmediateRef, Tag

from ._primitives import (
    _calc_store_expr,
    _compile_assignment_lines,
    _compile_expression_impl,
    _compile_guarded_instruction,
    _compile_indirect_value,
    _compile_value,
)


def _contact_tag_name(tag: Tag | ImmediateRef) -> str:
    if isinstance(tag, ImmediateRef):
        return tag.tag.name
    return tag.name


def _collect_helper_conditions(
    rung: LogicRung,
) -> list[tuple[Any, str, Any]]:
    """Walk a rung tree and collect instruction-internal conditions.

    Returns (instruction, field_name, condition) tuples for conditions that
    must be evaluated at rung entry (before any instructions execute) to match
    the core engine's ``instruction_condition_view`` snapshot semantics.
    """
    result: list[tuple[Any, str, Any]] = []

    def _walk(r: LogicRung) -> None:
        for item in r._execution_items:
            if isinstance(item, LogicRung):
                _walk(item)
                continue
            if isinstance(item, OnDelayInstruction):
                if item.reset_condition is not None:
                    result.append((item, "reset_condition", item.reset_condition))
            elif isinstance(item, CountUpInstruction):
                if item.reset_condition is not None:
                    result.append((item, "reset_condition", item.reset_condition))
                if item.down_condition is not None:
                    result.append((item, "down_condition", item.down_condition))
            elif isinstance(item, CountDownInstruction):
                if item.reset_condition is not None:
                    result.append((item, "reset_condition", item.reset_condition))
            elif isinstance(item, ShiftInstruction):
                result.append((item, "clock_condition", item.clock_condition))
                result.append((item, "reset_condition", item.reset_condition))
            elif isinstance(item, EventDrumInstruction):
                result.append((item, "reset_condition", item.reset_condition))
                if item.jump_condition is not None:
                    result.append((item, "jump_condition", item.jump_condition))
                if item.jog_condition is not None:
                    result.append((item, "jog_condition", item.jog_condition))
                for i, cond in enumerate(item.events):
                    result.append((item, f"events[{i}]", cond))
            elif isinstance(item, TimeDrumInstruction):
                result.append((item, "reset_condition", item.reset_condition))
                if item.jump_condition is not None:
                    result.append((item, "jump_condition", item.jump_condition))
                if item.jog_condition is not None:
                    result.append((item, "jog_condition", item.jog_condition))

    _walk(rung)
    return result


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
        return f'_rise(bool({tag_expr}), bool(_prev.get("{_contact_tag_name(cond.tag)}", False)))'
    if isinstance(cond, FallingEdgeCondition):
        ctx.mark_helper("_fall")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = ctx.symbol_for_tag(cond.tag)
        return f'_fall(bool({tag_expr}), bool(_prev.get("{_contact_tag_name(cond.tag)}", False)))'
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
    if isinstance(cond, ExprCompare):
        return f"({compile_expression(cond.left, ctx)} {cond.symbol} {compile_expression(cond.right, ctx)})"

    raise NotImplementedError(f"Unsupported condition type: {type(cond).__name__}")


def compile_expression(expr: Expression, ctx: CodegenContext) -> str:
    """Return a Python expression string with explicit parentheses."""
    return _compile_expression_impl(expr, ctx)


def _get_condition_snapshot(instr: Any, field_name: str, ctx: CodegenContext) -> str | None:
    """Return the rung-entry snapshot variable for a helper condition, if available."""
    entry = ctx._helper_condition_snapshots.get(id(instr))
    if entry is not None:
        snap = entry.get(field_name)
        if snap is not None:
            assert isinstance(snap, str)
            return snap
    return None


from ._instructions_basic import (
    _compile_call_instruction,
    _compile_copy_instruction,
    _compile_count_down_instruction,
    _compile_count_up_instruction,
    _compile_latch_instruction,
    _compile_off_delay_instruction,
    _compile_on_delay_instruction,
    _compile_out_instruction,
    _compile_reset_instruction,
    _compile_return_instruction,
)
from ._instructions_block import (
    _compile_blockcopy_instruction,
    _compile_event_drum_instruction,
    _compile_fill_instruction,
    _compile_search_instruction,
    _compile_shift_instruction,
    _compile_time_drum_instruction,
)
from ._instructions_pack import (
    _compile_pack_bits_instruction,
    _compile_pack_text_instruction,
    _compile_pack_words_instruction,
    _compile_unpack_bits_instruction,
    _compile_unpack_words_instruction,
)
from ._modbus import (
    _compile_modbus_receive_instruction,
    _compile_modbus_send_instruction,
)


def compile_instruction(
    instr: Any,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if isinstance(instr, OutInstruction):
        return _compile_out_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, LatchInstruction):
        return _compile_latch_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ResetInstruction):
        return _compile_reset_instruction(instr, enabled_expr, ctx, indent)
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
    if isinstance(instr, ModbusSendInstruction):
        return _compile_modbus_send_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ModbusReceiveInstruction):
        return _compile_modbus_receive_instruction(instr, enabled_expr, ctx, indent)

    loc = _source_location(instr)
    raise NotImplementedError(f"Unsupported instruction type: {type(instr).__name__} at {loc}")


def compile_rung(rung: LogicRung, fn_name: str, ctx: CodegenContext, indent: int = 0) -> list[str]:
    """Compile one rung into Python source lines."""
    previous = ctx._current_function
    ctx.set_current_function(fn_name)
    saved_snapshots = dict(ctx._helper_condition_snapshots)
    try:
        rung_id = ctx.next_name("rung")
        enabled_var = f"_{rung_id}_enabled"
        cond_expr = _compile_condition_group(rung._conditions, ctx)
        lines = [f"{' ' * indent}{enabled_var} = {cond_expr}"]

        helpers = _collect_helper_conditions(rung)
        if helpers:
            snap_idx = 0
            for instr, field_name, condition in helpers:
                snap_var = f"_{rung_id}_snap_{snap_idx}"
                snap_idx += 1
                expr = compile_condition(condition, ctx)
                lines.append(f"{' ' * indent}{snap_var} = {expr}")
                instr_id = id(instr)
                entry = ctx._helper_condition_snapshots.setdefault(instr_id, {})
                if field_name.startswith("events["):
                    event_list = entry.setdefault("events", [])
                    assert isinstance(event_list, list)
                    event_list.append(snap_var)
                else:
                    entry[field_name] = snap_var

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
        ctx._helper_condition_snapshots = saved_snapshots
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
