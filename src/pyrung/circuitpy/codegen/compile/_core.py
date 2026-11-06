"""Automatically generated module split."""

from __future__ import annotations

from keyword import iskeyword
from typing import Any

from pyrung.circuitpy.codegen._constants import (
    _FAULT_DIVISION_ERROR_TAG,
    _FAULT_MATH_OPERATION_ERROR_TAG,
)
from pyrung.circuitpy.codegen._util import (
    _bool_literal,
    _indent_body,
    _source_location,
    _store_coerce_expr,
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
    BreakInstruction,
    CalcInstruction,
    CallInstruction,
    ContinueInstruction,
    CopyInstruction,
    CountDownInstruction,
    CountUpInstruction,
    DebounceInstruction,
    EnabledFunctionCallInstruction,
    EventDrumInstruction,
    FillInstruction,
    FlasherInstruction,
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
    TimedInstruction,
    TimeDrumInstruction,
    TimeEventDrumInstruction,
    ToggleInstruction,
    UnpackToBitsInstruction,
    UnpackToWordsInstruction,
    control,
)
from pyrung.core.instruction.send_receive import (
    ModbusReceiveInstruction,
    ModbusSendInstruction,
)
from pyrung.core.kernel import PROVE_FOR_ITERATION_CAP
from pyrung.core.memory_block import IndirectExprRef, IndirectRef
from pyrung.core.rung import Rung as LogicRung
from pyrung.core.site_memory import edge_memory_key, edge_rows
from pyrung.core.storage import REAL_MAX
from pyrung.core.tag import ImmediateRef, Tag

from ._primitives import (
    CALC_FLAGS,
    _appends_flags,
    _calc_store_expr,
    _compile_assignment_lines,
    _compile_expression_impl,
    _compile_guarded_instruction,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _snapshot_tag_symbol,
)


def _edge_locals(memory_key: str) -> tuple[str, str]:
    """The locals holding an edge site's bit this row and its memory before it."""
    base = "_" + "".join(ch if ch.isalnum() else "_" for ch in memory_key.lstrip("_"))
    return f"{base}_now", f"{base}_was"


def _compile_edge_row(row: Any, ctx: CodegenContext, indent: int) -> list[str]:
    """Scan the edge sites of one row where the solve reaches it: read each
    memory, then store the bit seen.

    Every edge contact in the row stores the bit it sees whether or not power
    reaches it (CLICK updates an edge contact's memory whenever it is scanned),
    and compares against the memory as it was before the row: the row's edge
    expressions read the ``_was`` locals.  The bit is read live, so it sees
    the writes of the rows above.
    """
    sites = ctx._edge_rows.get(id(row), ())
    sp = " " * indent
    lines: list[str] = []
    for key, edge in sites:
        now, was = _edge_locals(key)
        tag_expr = _snapshot_tag_symbol(edge.tag, ctx)
        lines.append(f"{sp}{now} = bool({tag_expr})")
        lines.append(f"{sp}{was} = _mem.get({key!r}, False)")
        # Stored only when it changes: an unwritten memory reads off, as in
        # the interpreter, whose state holds no entry until a contact sees on.
        lines.append(f"{sp}if {now} != {was}:")
        lines.append(f"{sp}    _mem[{key!r}] = {now}")
    return lines


def _box_helper_conditions(item: Any) -> list[tuple[Any, str, Any]]:
    """The helper legs (pins) of one instruction box, in the order its compiler reads them.

    Returns (instruction, field_name, condition) tuples; empty for a box
    without legs.
    """
    result: list[tuple[Any, str, Any]] = []
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
    elif isinstance(item, (TimeDrumInstruction, TimeEventDrumInstruction)):
        result.append((item, "reset_condition", item.reset_condition))
        if item.jump_condition is not None:
            result.append((item, "jump_condition", item.jump_condition))
        if item.jog_condition is not None:
            result.append((item, "jog_condition", item.jog_condition))
        if isinstance(item, TimeEventDrumInstruction):
            # Only the steps that have an event; the compiler reads them in order.
            for i, cond in enumerate(item.events):
                if cond is not None:
                    result.append((item, f"events[{i}]", cond))
    return result


def _bind_helper_legs(
    helpers: list[tuple[Any, str, Any]],
    prefix: str,
    ctx: CodegenContext,
) -> list[str]:
    """Compile one box's helper legs into ``_{prefix}_snap_K`` locals.

    Returns the binding lines (unindented).  The caller emits them just
    before the box's code, so the box reads all its pins at one moment, as it
    starts and before any write of its own (the interpreter's
    ``instruction_condition_view``); the box compilers find the locals via
    ``_get_condition_snapshot``.
    """
    lines: list[str] = []
    for snap_idx, (instr, field_name, condition) in enumerate(helpers):
        snap_var = f"_{prefix}_snap_{snap_idx}"
        lines.append(f"{snap_var} = {compile_condition(condition, ctx)}")
        entry = ctx._helper_condition_snapshots.setdefault(id(instr), {})
        if field_name.startswith("events["):
            event_list = entry.setdefault("events", [])
            assert isinstance(event_list, list)
            event_list.append(snap_var)
        else:
            entry[field_name] = snap_var
    return lines


def _compile_box(item: Any, enabled_expr: str, ctx: CodegenContext, indent: int) -> list[str]:
    """Compile one box where the solve reaches it.

    A box with helper legs (pins) reads them all as it starts, enabled or
    not and before any write of its own: its edge pins scan there and its
    legs bind to locals just before its code, as the interpreter's
    ``instruction_condition_view`` does.
    """
    helpers = _box_helper_conditions(item)
    if not helpers:
        return compile_instruction(item, enabled_expr, ctx, indent)
    sp = " " * indent
    lines = _compile_edge_row(item, ctx, indent)
    saved_snapshots = dict(ctx._helper_condition_snapshots)
    try:
        legs = _bind_helper_legs(helpers, ctx.next_name("box"), ctx)
        lines.extend(f"{sp}{line}" for line in legs)
        lines.extend(compile_instruction(item, enabled_expr, ctx, indent))
    finally:
        ctx._helper_condition_snapshots = saved_snapshots
    return lines


def compile_condition(cond: Condition, ctx: CodegenContext) -> str:
    """Return a Python boolean expression string.

    Contacts read the tags live: a row's contacts are compiled where the
    solve reaches the row, so they see the writes of the rows above it.
    """

    if isinstance(cond, BitCondition):
        return f"bool({_snapshot_tag_symbol(cond.tag, ctx)})"
    if isinstance(cond, NormallyClosedCondition):
        return f"(not bool({_snapshot_tag_symbol(cond.tag, ctx)}))"
    if isinstance(cond, IntTruthyCondition):
        return f"(int({_snapshot_tag_symbol(cond.tag, ctx)}) != 0)"
    if isinstance(cond, CompareEq):
        return (
            f"({_compile_value(cond.tag, ctx, condition=True)}"
            f" == {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, CompareNe):
        return (
            f"({_compile_value(cond.tag, ctx, condition=True)}"
            f" != {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, CompareLt):
        return (
            f"({_compile_value(cond.tag, ctx, condition=True)}"
            f" < {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, CompareLe):
        return (
            f"({_compile_value(cond.tag, ctx, condition=True)}"
            f" <= {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, CompareGt):
        return (
            f"({_compile_value(cond.tag, ctx, condition=True)}"
            f" > {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, CompareGe):
        return (
            f"({_compile_value(cond.tag, ctx, condition=True)}"
            f" >= {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, AllCondition):
        parts = [compile_condition(child, ctx) for child in cond.conditions]
        return "(" + " and ".join(parts) + ")" if parts else "True"
    if isinstance(cond, AnyCondition):
        parts = [compile_condition(child, ctx) for child in cond.conditions]
        return "(" + " or ".join(parts) + ")" if parts else "False"
    if isinstance(cond, RisingEdgeCondition):
        now, was = _edge_locals(edge_memory_key(cond))
        return f"({now} and not {was})"
    if isinstance(cond, FallingEdgeCondition):
        now, was = _edge_locals(edge_memory_key(cond))
        return f"({was} and not {now})"
    if isinstance(cond, IndirectCompareEq):
        return (
            f"({_compile_value(cond.indirect_ref, ctx, condition=True)}"
            f" == {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, IndirectCompareNe):
        return (
            f"({_compile_value(cond.indirect_ref, ctx, condition=True)}"
            f" != {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, IndirectCompareLt):
        return (
            f"({_compile_value(cond.indirect_ref, ctx, condition=True)}"
            f" < {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, IndirectCompareLe):
        return (
            f"({_compile_value(cond.indirect_ref, ctx, condition=True)}"
            f" <= {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, IndirectCompareGt):
        return (
            f"({_compile_value(cond.indirect_ref, ctx, condition=True)}"
            f" > {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, IndirectCompareGe):
        return (
            f"({_compile_value(cond.indirect_ref, ctx, condition=True)}"
            f" >= {_compile_value(cond.value, ctx, condition=True)})"
        )
    if isinstance(cond, ExprCompare):
        return (
            f"({compile_expression(cond.left, ctx)} "
            f"{cond.symbol} {compile_expression(cond.right, ctx)})"
        )

    raise NotImplementedError(f"Unsupported condition type: {type(cond).__name__}")


def compile_expression(
    expr: Expression,
    ctx: CodegenContext,
    *,
    strict: bool = False,
) -> str:
    """Return a Python expression string with explicit parentheses.

    *strict* for calc's expression, which raises on a math error; anywhere
    else an operation that can raise gives 0 instead (permissive).
    """
    return _compile_expression_impl(expr, ctx, strict=strict)


def _get_condition_snapshot(instr: Any, field_name: str, ctx: CodegenContext) -> str | None:
    """Return the local a box's helper leg is bound to at box entry, if any."""
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
    _compile_drum_instruction,
    _compile_fill_instruction,
    _compile_search_instruction,
    _compile_shift_instruction,
)
from ._instructions_out import (
    _compile_debounce_instruction,
    _compile_flasher_instruction,
    _compile_timed_instruction,
    _compile_toggle_instruction,
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
    if isinstance(instr, ToggleInstruction):
        return _compile_toggle_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, DebounceInstruction):
        return _compile_debounce_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, FlasherInstruction):
        return _compile_flasher_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, TimedInstruction):
        return _compile_timed_instruction(instr, enabled_expr, ctx, indent)
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
    if isinstance(instr, (EventDrumInstruction, TimeDrumInstruction, TimeEventDrumInstruction)):
        return _compile_drum_instruction(instr, enabled_expr, ctx, indent)
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
    if isinstance(instr, BreakInstruction):
        return _compile_loop_exit_instruction("break", enabled_expr, indent)
    if isinstance(instr, ContinueInstruction):
        return _compile_loop_exit_instruction("continue", enabled_expr, indent)
    if isinstance(instr, ForLoopInstruction):
        return _compile_for_loop_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ModbusSendInstruction):
        return _compile_modbus_send_instruction(instr, enabled_expr, ctx, indent)
    if isinstance(instr, ModbusReceiveInstruction):
        return _compile_modbus_receive_instruction(instr, enabled_expr, ctx, indent)

    loc = _source_location(instr)
    raise NotImplementedError(f"Unsupported instruction type: {type(instr).__name__} at {loc}")


def compile_rung(
    rung: LogicRung,
    fn_name: str,
    ctx: CodegenContext,
    indent: int = 0,
    *,
    local_conditions: bool = False,
) -> list[str]:
    """Compile one rung into Python source lines, in instruction order.

    The trunk's contacts are read once, as the rung starts; each branch's own
    contacts are read where the solve reaches the branch (after the writes of
    the rows above it), and each box reads its pins as it starts.
    ``local_conditions`` compiles only the rung's own contacts (a forloop body
    rung, whose inherited loop-rung contacts are already true).
    """
    previous = ctx._current_function
    ctx.set_current_function(fn_name)
    try:
        rung_id = ctx.next_name("rung")
        enabled_var = f"_{rung_id}_enabled"
        conditions = (
            rung._conditions[rung._branch_condition_start :]
            if local_conditions
            else rung._conditions
        )
        cond_expr = "True" if ctx.force_rung_enable else _compile_condition_group(conditions, ctx)
        lines = _compile_edge_row(rung, ctx, indent)
        lines.append(f"{' ' * indent}{enabled_var} = {cond_expr}")
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
    """Compile a rung's items in solve order.

    A branch is solved where it sits: its edge sites scan and its own
    contacts are read there, under *enabled_expr* (the power of the row it
    hangs from), then its items follow.
    """
    lines: list[str] = []
    sp = " " * indent

    def _solve(item: LogicRung, branch_var: str) -> list[str]:
        local_conditions = item._conditions[item._branch_condition_start :]
        local_expr = (
            "True" if ctx.force_rung_enable else _compile_condition_group(local_conditions, ctx)
        )
        return [
            *_compile_edge_row(item, ctx, indent),
            f"{sp}{branch_var} = ({enabled_expr} and ({local_expr}))",
        ]

    branch_idx = 0
    for item in rung._execution_items:
        if isinstance(item, LogicRung):
            branch_var = f"_{scope_key}_branch_{branch_idx}"
            child_scope = f"{scope_key}_b{branch_idx}"
            branch_idx += 1
            lines.extend(_solve(item, branch_var))
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
        lines.extend(_compile_box(item, enabled_expr, ctx, indent))
    return lines


def compile_rungs(
    rungs: list[LogicRung],
    fn_name: str,
    ctx: CodegenContext,
    indent: int = 0,
) -> list[str]:
    """Compile a sequence of top-level rungs.

    A ``.continued()`` rung is a rung of its own: it is only drawn as a
    further wire of the rung above it.
    """
    lines: list[str] = []
    for rung in rungs:
        ctx._edge_rows.update(edge_rows(rung))
        lines.extend(compile_rung(rung, fn_name, ctx, indent=indent))
    return lines


def _compile_condition_group(conditions: list[Condition], ctx: CodegenContext) -> str:
    if not conditions:
        return "True"
    return " and ".join(compile_condition(cond, ctx) for cond in conditions)


def _calc_range_condition(value_var: str, dest_type: str, mode: str) -> str | None:
    if mode == "hex":
        return f"int({value_var}) < 0 or int({value_var}) > 0xFFFF"
    if dest_type == "INT":
        return f"int({value_var}) < -32768 or int({value_var}) > 32767"
    if dest_type == "DINT":
        return f"int({value_var}) < -2147483648 or int({value_var}) > 2147483647"
    if dest_type == "WORD":
        return f"int({value_var}) < 0 or int({value_var}) > 65535"
    if dest_type == "REAL":
        # A comparison with NaN is false, so NaN does not set the flag.
        return f"{value_var} > {REAL_MAX!r} or {value_var} < {-REAL_MAX!r}"
    return None


def _compile_calc_instruction(
    instr: CalcInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    ctx.mark_helper("_calc_math_isfinite")
    div_err = ctx.symbol_if_referenced(_FAULT_DIVISION_ERROR_TAG)
    value_expr = _compile_value(instr.expression, ctx, strict=True)
    store_expr = _calc_store_expr("_calc_value", instr.dest.type.name, instr.mode, ctx)
    zero_store_expr = _calc_store_expr("0", instr.dest.type.name, instr.mode, ctx)
    out_of_range_body = _compile_set_out_of_range_fault_body(ctx)
    math_err = ctx.symbol_if_referenced(_FAULT_MATH_OPERATION_ERROR_TAG)
    # A failing operation is 0 and appends its flag's code to _calc_flags: the
    # rest of the formula runs (core CALC_FAULTS), and its flags are set after.
    flagged = _appends_flags(value_expr)
    enabled_body = [f"{CALC_FLAGS} = []"] if flagged else []
    set_flags: list[str] = []
    if flagged:
        if div_err:
            set_flags += [f"    if 40 in {CALC_FLAGS}:", f"        {div_err} = True"]
        if math_err:
            set_flags += [f"    if 46 in {CALC_FLAGS}:", f"        {math_err} = True"]
        if out_of_range_body != ["pass"]:
            set_flags.append(f"    if 43 in {CALC_FLAGS}:")
            set_flags.extend(f"        {line}" for line in out_of_range_body)
        if set_flags:
            # One test on the path with no error.
            set_flags.insert(0, f"if {CALC_FLAGS}:")
    if set_flags and isinstance(instr.expression, (IndirectRef, IndirectExprRef)):
        # An address below its block raises IndexError (nothing written, the
        # guard's address-error flag): the flags of its failing operations first.
        enabled_body += ["try:", f"    _calc_value = {value_expr}", "finally:"]
        enabled_body.extend(f"    {line}" for line in set_flags)
    else:
        enabled_body.append(f"_calc_value = {value_expr}")
        enabled_body.extend(set_flags)
    dest_type = instr.dest.type.name
    range_cond = _calc_range_condition("_calc_value", dest_type, instr.mode)
    if dest_type == "REAL" and instr.mode != "hex":
        # Past CLICK's REAL range the result is +/-Inf, with the out-of-range
        # flag, as a float32 Math stores it; NaN is kept, with no flag.
        enabled_body.append(f"if {range_cond}:")
        enabled_body.extend(f"    {line}" for line in out_of_range_body if line != "pass")
        enabled_body += [
            f"    if _calc_value > {REAL_MAX!r}:",
            '        _calc_value = float("inf")',
            f"    elif _calc_value < {-REAL_MAX!r}:",
            '        _calc_value = float("-inf")',
        ]
        range_cond = None
    else:
        # An Inf or NaN result (from an Inf or NaN operand) stores 0; into an
        # integer register +/-Inf sets the out-of-range flag, and NaN does not.
        flags_non_finite = range_cond is not None and out_of_range_body != ["pass"]
        enabled_body.append("if isinstance(_calc_value, float) and not math.isfinite(_calc_value):")
        if flags_non_finite:
            enabled_body.append("    if _calc_value == _calc_value:")
            enabled_body.extend(f"        {line}" for line in out_of_range_body)
        enabled_body.append("    _calc_value = 0")
    if range_cond and out_of_range_body != ["pass"]:
        enabled_body.append(f"if {range_cond}:")
        enabled_body.extend(f"    {line}" for line in out_of_range_body)
    enabled_body.append("try:")
    enabled_body.extend(_compile_assignment_lines(instr.dest, store_expr, ctx, indent=4))
    enabled_body.append("except OverflowError:")
    enabled_body.extend(_indent_body(out_of_range_body, 4))
    enabled_body.extend(_compile_assignment_lines(instr.dest, zero_store_expr, ctx, indent=4))
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _in_quotes(text: str) -> str:
    """*text* as the inside of a generated double-quoted string."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _in_f_string(text: str) -> str:
    """*text* as the inside of a generated double-quoted f-string."""
    return _in_quotes(text).replace("{", "{{").replace("}", "}}")


def _compile_function_kwargs(ins: dict[str, Any], ctx: CodegenContext) -> str:
    """A function call's keyword arguments, in the order stepping passes them.

    A name that is no Python keyword argument (``class``, ``a b``, ``__debug__``, the one
    identifier the compiler won't take as a keyword and ``iskeyword`` doesn't list) goes
    through ``**``, and so does one outside ASCII, which the compiler would normalize.
    """
    compiled = [(name, _compile_function_arg(value, ctx)) for name, value in ins.items()]
    if all(
        name.isascii() and name.isidentifier() and not iskeyword(name) and name != "__debug__"
        for name in ins
    ):
        return ", ".join(f"{name}={expr}" for name, expr in compiled)
    return "**{" + ", ".join(f"{name!r}: {expr}" for name, expr in compiled) + "}"


def _call_result_var(ctx: CodegenContext) -> str:
    """A local for what a call returned, in no namespace a function's symbol (``_fn_*``) is."""
    return f"_{ctx.next_name('call_result')}"


def _compile_function_arg(value: Any, ctx: CodegenContext) -> str:
    if isinstance(value, ImmediateRef):
        return _compile_function_arg(value.value, ctx)
    if isinstance(value, Tag | IndirectRef | IndirectExprRef | Expression):
        return _compile_value(value, ctx)
    return ctx.function_constant(value)


def _compile_function_call_instruction(
    instr: FunctionCallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    fn_symbol = ctx.register_function_source(instr._fn)
    fn_name = getattr(instr._fn, "__name__", type(instr._fn).__name__)
    result_var = _call_result_var(ctx)
    kwargs = _compile_function_kwargs(instr._ins, ctx)
    call_expr = f"{fn_symbol}({kwargs})" if kwargs else f"{fn_symbol}()"
    enabled_body = [f"{result_var} = {call_expr}"]
    if instr._outs:
        enabled_body.extend(
            [
                f"if {result_var} is None:",
                f'    raise TypeError("{_in_quotes(f"run_function: {fn_name!r} returned None but outs were declared")}")',
            ]
        )
        for key, target in instr._outs.items():
            target_type = _value_type_name(target)
            enabled_body.extend(
                [
                    f"if {key!r} not in {result_var}:",
                    "    raise KeyError(",
                    f'        f"{_in_f_string(f"run_function: {fn_name!r} missing key {key!r}; got ")}{{sorted({result_var})}}"',
                    "    )",
                ]
            )
            value_expr = _store_coerce_expr(f"{result_var}[{key!r}]", target_type, ctx)
            enabled_body.extend(_compile_assignment_lines(target, value_expr, ctx, indent=0))
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_enabled_function_call_instruction(
    instr: EnabledFunctionCallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    fn_symbol = ctx.register_function_source(instr._fn)
    result_var = _call_result_var(ctx)
    kwargs = _compile_function_kwargs(instr._ins, ctx)
    call_args = enabled_expr
    if kwargs:
        call_args += f", {kwargs}"
    lines = [f"{' ' * indent}{result_var} = {fn_symbol}({call_args})"]
    if instr._outs:
        fn_name = getattr(instr._fn, "__name__", type(instr._fn).__name__)
        lines.extend(
            [
                f"{' ' * indent}if {result_var} is None:",
                f'{" " * (indent + 4)}raise TypeError("{_in_quotes(f"run_enabled_function: {fn_name!r} returned None but outs were declared")}")',
            ]
        )
        for key, target in instr._outs.items():
            target_type = _value_type_name(target)
            lines.extend(
                [
                    f"{' ' * indent}if {key!r} not in {result_var}:",
                    f"{' ' * (indent + 4)}raise KeyError(",
                    f'{" " * (indent + 8)}f"{_in_f_string(f"run_enabled_function: {fn_name!r} missing key {key!r}; got ")}{{sorted({result_var})}}"',
                    f"{' ' * (indent + 4)})",
                ]
            )
            value_expr = _store_coerce_expr(f"{result_var}[{key!r}]", target_type, ctx)
            lines.extend(_compile_assignment_lines(target, value_expr, ctx, indent=indent))
    return lines


def _compile_for_loop_instruction(
    instr: ForLoopInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    count_expr = _compile_value(instr.count, ctx)
    depth = ctx._for_loop_depth
    suffix = "" if depth == 0 else f"_{depth + 1}"
    iterations_var = f"_iterations{suffix}"
    index_var = f"_for_i{suffix}"
    ctx._for_loop_depth = depth + 1
    try:
        children = _compile_loop_body(instr, ctx)
    finally:
        ctx._for_loop_depth = depth
    # CLICK reads a Dint count whole and signed, so 0 or below runs none, and
    # the rest as unsigned 16-bit (control.for_loop_iterations); a count of 0
    # runs FOR_ZERO_COUNT_ITERATIONS iterations.
    non_finite = "fault.out_of_range" in instr._fault_writes
    if non_finite:
        # A REAL count holding Inf or NaN runs no iteration and sets SC43
        # (ForLoopInstruction.iterations); _bit_int turns NaN into OverflowError.
        ctx.mark_helper("_bit_int")
        count_expr = f"_bit_int({count_expr})"
    mask = control.for_count_mask(instr.count)
    iterations_lines = [f"{iterations_var} = int({count_expr}) & 0x{mask:X}"]
    zero_runs = int(control.FOR_ZERO_COUNT_ITERATIONS)
    if mask == control.FOR_COUNT_MASK_32:
        # Past FOR_COUNT_MAX_32 the bits are a negative count: none.
        kept = f"({iterations_var} or {zero_runs})" if zero_runs else iterations_var
        iterations_lines.append(
            f"{iterations_var} = 0 if {iterations_var} > 0x{control.FOR_COUNT_MAX_32:X} else {kept}"
        )
    elif zero_runs:
        iterations_lines.append(f"{iterations_var} = {iterations_var} or {zero_runs}")
    if non_finite:
        iterations_lines = [
            "try:",
            *_indent_body(iterations_lines, 4),
            "except OverflowError:",
            f"    {iterations_var} = 0",
            *_indent_body(_compile_set_out_of_range_fault_body(ctx), 4),
        ]
    if ctx.proof_metadata and mask > control.FOR_COUNT_MASK_16:
        # The verifier's kernel stops a 32-bit count past its cap rather than
        # run it (a Dint up to 2147483647 runs that many iterations).  The source
        # names the loop by index: its file and line stay out of the lock's hash.
        site = len(ctx.for_cap_sites)
        ctx.for_cap_sites.append(_for_count_site(instr))
        iterations_lines += [
            f"if {iterations_var} > {PROVE_FOR_ITERATION_CAP}:",
            f"    raise _ForIterationsBeyondCap(*_for_cap_sites[{site}], {iterations_var})",
        ]
    body = [
        *iterations_lines,
        f"for {index_var} in range({iterations_var}):",
        *_indent_body(_compile_assignment_lines(instr.idx_tag, index_var, ctx, indent=0), 4),
        *_indent_body(children, 4),
    ]
    # A false For rung skips its body: only the For's own one-shot re-arms.
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, body)


def _for_count_site(instr: ForLoopInstruction) -> tuple[str, str, tuple[str, ...]]:
    """Where a For is, how its count reads, and the tags it reads (for a refusal)."""
    count = instr.count
    if isinstance(count, Tag):
        return _source_location(instr), count.name, (count.name,)
    if isinstance(count, IndirectRef):
        return (
            _source_location(instr),
            f"{count.block.name}[{count.pointer.name}]",
            (count.pointer.name,),
        )
    if isinstance(count, IndirectExprRef):
        return _source_location(instr), f"{count.block.name}[...]", ()
    return _source_location(instr), str(count), ()


def _compile_loop_body(
    instr: ForLoopInstruction,
    ctx: CodegenContext,
) -> list[str]:
    """Compile one iteration of a loop body: bare instructions and body rungs."""
    lines: list[str] = []
    for item in instr.body:
        if not isinstance(item, LogicRung):
            # A bare body box is a row of its own: on every iteration it reads
            # its pins as it starts, before it writes (``_compile_box``).
            lines.extend(_compile_box(item, "True", ctx, 0))
        else:
            # A body rung reads live tags: its contacts are evaluated afresh
            # on every iteration, as a rung of its own.
            fn_name = ctx._current_function
            assert fn_name is not None
            lines.extend(compile_rung(item, fn_name, ctx, indent=0, local_conditions=True))
    return lines


def _compile_loop_exit_instruction(keyword: str, enabled_expr: str, indent: int) -> list[str]:
    sp = " " * indent
    enabled_literal = _bool_literal(enabled_expr)
    if enabled_literal is False:
        return []
    if enabled_literal is True:
        return [f"{sp}{keyword}"]
    return [f"{sp}if {enabled_expr}:", f"{sp}    {keyword}"]
