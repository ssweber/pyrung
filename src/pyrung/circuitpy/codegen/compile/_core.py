"""Automatically generated module split."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pyrung.circuitpy.codegen._constants import _FAULT_DIVISION_ERROR_TAG
from pyrung.circuitpy.codegen._util import (
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
from pyrung.core.memory_block import BlockRange, IndirectBlockRange, IndirectExprRef, IndirectRef
from pyrung.core.rung import Rung as LogicRung
from pyrung.core.tag import ImmediateRef, Tag
from pyrung.core.validation.walker import _condition_children

from ._primitives import (
    _calc_store_expr,
    _compile_assignment_lines,
    _compile_expression_impl,
    _compile_guarded_instruction,
    _compile_indirect_value,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _snapshot_tag_symbol,
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


@dataclass(frozen=True)
class _ConditionSnapshotBindings:
    scalar_symbols: dict[str, str]
    block_symbols: dict[int, str]


def _collect_snapshot_refs(
    value: Any,
    ctx: CodegenContext,
    *,
    scalar_tags: set[str],
    block_ids: set[int],
    seen: set[int],
) -> None:
    if value is None or isinstance(value, (bool, int, float, str, bytes, bytearray)):
        return

    value_id = id(value)
    if value_id in seen:
        return
    seen.add(value_id)

    if isinstance(value, ImmediateRef):
        _collect_snapshot_refs(
            value.value,
            ctx,
            scalar_tags=scalar_tags,
            block_ids=block_ids,
            seen=seen,
        )
        return

    if isinstance(value, Tag):
        block_info = ctx.tag_block_addresses.get(value.name)
        if block_info is not None:
            block_ids.add(block_info[0])
        else:
            scalar_tags.add(value.name)
        return

    if isinstance(value, IndirectRef):
        block_ids.add(id(value.block))
        _collect_snapshot_refs(
            value.pointer,
            ctx,
            scalar_tags=scalar_tags,
            block_ids=block_ids,
            seen=seen,
        )
        return

    if isinstance(value, IndirectExprRef):
        block_ids.add(id(value.block))
        _collect_snapshot_refs(
            value.expr,
            ctx,
            scalar_tags=scalar_tags,
            block_ids=block_ids,
            seen=seen,
        )
        return

    if isinstance(value, BlockRange):
        block_ids.add(id(value.block))
        return

    if isinstance(value, IndirectBlockRange):
        block_ids.add(id(value.block))
        _collect_snapshot_refs(
            value.start_expr,
            ctx,
            scalar_tags=scalar_tags,
            block_ids=block_ids,
            seen=seen,
        )
        _collect_snapshot_refs(
            value.end_expr,
            ctx,
            scalar_tags=scalar_tags,
            block_ids=block_ids,
            seen=seen,
        )
        return

    if isinstance(value, Condition):
        for _, child in _condition_children(value):
            _collect_snapshot_refs(
                child,
                ctx,
                scalar_tags=scalar_tags,
                block_ids=block_ids,
                seen=seen,
            )
        return

    if isinstance(value, Expression):
        for key in sorted(vars(value)):
            if key.startswith("_"):
                continue
            _collect_snapshot_refs(
                getattr(value, key),
                ctx,
                scalar_tags=scalar_tags,
                block_ids=block_ids,
                seen=seen,
            )
        return

    if isinstance(value, dict):
        for key in sorted(value, key=repr):
            _collect_snapshot_refs(
                value[key],
                ctx,
                scalar_tags=scalar_tags,
                block_ids=block_ids,
                seen=seen,
            )
        return

    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_snapshot_refs(
                item,
                ctx,
                scalar_tags=scalar_tags,
                block_ids=block_ids,
                seen=seen,
            )


def _collect_rung_snapshot_bindings(
    rungs: list[LogicRung],
    ctx: CodegenContext,
    *,
    indent: int,
) -> tuple[list[str], _ConditionSnapshotBindings]:
    scalar_tags: set[str] = set()
    block_ids: set[int] = set()
    seen: set[int] = set()

    def _walk_rung(rung: LogicRung) -> None:
        for condition in rung._conditions:
            _collect_snapshot_refs(
                condition,
                ctx,
                scalar_tags=scalar_tags,
                block_ids=block_ids,
                seen=seen,
            )
        for item in rung._execution_items:
            if isinstance(item, LogicRung):
                _walk_rung(item)

    for rung in rungs:
        _walk_rung(rung)
        for _instr, _field_name, condition in _collect_helper_conditions(rung):
            _collect_snapshot_refs(
                condition,
                ctx,
                scalar_tags=scalar_tags,
                block_ids=block_ids,
                seen=seen,
            )

    lines: list[str] = []
    scalar_symbols: dict[str, str] = {}
    block_symbols: dict[int, str] = {}
    sp = " " * indent

    for tag_name in sorted(scalar_tags):
        tag = ctx.referenced_tags.get(tag_name)
        if tag is None:
            continue
        snap_var = f"_{ctx.next_name('cond_snap')}"
        scalar_symbols[tag_name] = snap_var
        lines.append(f"{sp}{snap_var} = {_snapshot_tag_symbol(tag, ctx)}")

    for block_id in sorted(block_ids, key=lambda bid: ctx.block_symbols.get(bid, "")):
        binding = ctx.block_bindings.get(block_id)
        if binding is None:
            continue
        snap_var = f"_{ctx.next_name('cond_block_snap')}"
        block_symbols[block_id] = snap_var
        if ctx.blockless:
            name_symbol = ctx.block_name_tuple_symbol(block_id)
            default = binding.block._get_tag(binding.start).default
            lines.append(
                f"{sp}{snap_var} = [tags.get(_name, {default!r}) for _name in {name_symbol}]"
            )
        else:
            lines.append(f"{sp}{snap_var} = list({ctx.symbol_for_block(binding.block)})")

    return lines, _ConditionSnapshotBindings(
        scalar_symbols=scalar_symbols,
        block_symbols=block_symbols,
    )


def compile_condition(
    cond: Condition,
    ctx: CodegenContext,
    *,
    condition_snapshot: _ConditionSnapshotBindings | None = None,
) -> str:
    """Return a Python boolean expression string."""
    scalar_snapshots = None
    block_snapshots = None
    if condition_snapshot is not None:
        scalar_snapshots = condition_snapshot.scalar_symbols
        block_snapshots = condition_snapshot.block_symbols

    if isinstance(cond, BitCondition):
        return (
            "bool("
            f"{_snapshot_tag_symbol(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            ")"
        )
    if isinstance(cond, NormallyClosedCondition):
        return (
            "(not bool("
            f"{_snapshot_tag_symbol(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            "))"
        )
    if isinstance(cond, IntTruthyCondition):
        return (
            "(int("
            f"{_snapshot_tag_symbol(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            ") != 0)"
        )
    if isinstance(cond, CompareEq):
        return (
            f"({_compile_value(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" == {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, CompareNe):
        return (
            f"({_compile_value(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" != {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, CompareLt):
        return (
            f"({_compile_value(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" < {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, CompareLe):
        return (
            f"({_compile_value(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" <= {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, CompareGt):
        return (
            f"({_compile_value(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" > {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, CompareGe):
        return (
            f"({_compile_value(cond.tag, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" >= {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, AllCondition):
        parts = [
            compile_condition(child, ctx, condition_snapshot=condition_snapshot)
            for child in cond.conditions
        ]
        return "(" + " and ".join(parts) + ")" if parts else "True"
    if isinstance(cond, AnyCondition):
        parts = [
            compile_condition(child, ctx, condition_snapshot=condition_snapshot)
            for child in cond.conditions
        ]
        return "(" + " or ".join(parts) + ")" if parts else "False"
    if isinstance(cond, RisingEdgeCondition):
        ctx.mark_helper("_rise")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = _snapshot_tag_symbol(
            cond.tag,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        return f'_rise(bool({tag_expr}), bool(_prev.get("{_contact_tag_name(cond.tag)}", False)))'
    if isinstance(cond, FallingEdgeCondition):
        ctx.mark_helper("_fall")
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_prev")
        tag_expr = _snapshot_tag_symbol(
            cond.tag,
            ctx,
            scalar_snapshots=scalar_snapshots,
            block_snapshots=block_snapshots,
        )
        return f'_fall(bool({tag_expr}), bool(_prev.get("{_contact_tag_name(cond.tag)}", False)))'
    if isinstance(cond, IndirectCompareEq):
        return (
            f"({_compile_indirect_value(cond.indirect_ref, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" == {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, IndirectCompareNe):
        return (
            f"({_compile_indirect_value(cond.indirect_ref, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" != {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, IndirectCompareLt):
        return (
            f"({_compile_indirect_value(cond.indirect_ref, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" < {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, IndirectCompareLe):
        return (
            f"({_compile_indirect_value(cond.indirect_ref, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" <= {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, IndirectCompareGt):
        return (
            f"({_compile_indirect_value(cond.indirect_ref, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" > {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, IndirectCompareGe):
        return (
            f"({_compile_indirect_value(cond.indirect_ref, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)}"
            f" >= {_compile_value(cond.value, ctx, scalar_snapshots=scalar_snapshots, block_snapshots=block_snapshots)})"
        )
    if isinstance(cond, ExprCompare):
        return (
            f"({compile_expression(cond.left, ctx, condition_snapshot=condition_snapshot)} "
            f"{cond.symbol} {compile_expression(cond.right, ctx, condition_snapshot=condition_snapshot)})"
        )

    raise NotImplementedError(f"Unsupported condition type: {type(cond).__name__}")


def compile_expression(
    expr: Expression,
    ctx: CodegenContext,
    *,
    condition_snapshot: _ConditionSnapshotBindings | None = None,
) -> str:
    """Return a Python expression string with explicit parentheses."""
    scalar_snapshots = None
    block_snapshots = None
    if condition_snapshot is not None:
        scalar_snapshots = condition_snapshot.scalar_symbols
        block_snapshots = condition_snapshot.block_symbols
    return _compile_expression_impl(
        expr,
        ctx,
        scalar_snapshots=scalar_snapshots,
        block_snapshots=block_snapshots,
    )


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


def compile_rung(
    rung: LogicRung,
    fn_name: str,
    ctx: CodegenContext,
    indent: int = 0,
    *,
    condition_snapshot: _ConditionSnapshotBindings | None = None,
) -> list[str]:
    """Compile one rung into Python source lines."""
    previous = ctx._current_function
    ctx.set_current_function(fn_name)
    saved_snapshots = dict(ctx._helper_condition_snapshots)
    try:
        rung_id = ctx.next_name("rung")
        enabled_var = f"_{rung_id}_enabled"
        cond_expr = (
            "True"
            if ctx.force_rung_enable
            else _compile_condition_group(
                rung._conditions,
                ctx,
                condition_snapshot=condition_snapshot,
            )
        )
        lines = [f"{' ' * indent}{enabled_var} = {cond_expr}"]

        helpers = _collect_helper_conditions(rung)
        if helpers:
            snap_idx = 0
            for instr, field_name, condition in helpers:
                snap_var = f"_{rung_id}_snap_{snap_idx}"
                snap_idx += 1
                expr = compile_condition(
                    condition,
                    ctx,
                    condition_snapshot=condition_snapshot,
                )
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
                condition_snapshot=condition_snapshot,
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
    *,
    condition_snapshot: _ConditionSnapshotBindings | None = None,
) -> list[str]:
    lines: list[str] = []
    branch_vars: dict[int, str] = {}
    branch_idx = 0
    for item in rung._execution_items:
        if not isinstance(item, LogicRung):
            continue
        local_conditions = item._conditions[item._branch_condition_start :]
        local_expr = (
            "True"
            if ctx.force_rung_enable
            else _compile_condition_group(
                local_conditions,
                ctx,
                condition_snapshot=condition_snapshot,
            )
        )
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
                    condition_snapshot=condition_snapshot,
                )
            )
            continue
        lines.extend(compile_instruction(item, enabled_expr, ctx, indent))
    return lines


def compile_rungs(
    rungs: list[LogicRung],
    fn_name: str,
    ctx: CodegenContext,
    indent: int = 0,
) -> list[str]:
    """Compile a sequence of top-level rungs, preserving continued() chains."""
    lines: list[str] = []
    i = 0
    while i < len(rungs):
        chain = [rungs[i]]
        i += 1
        while i < len(rungs) and rungs[i]._use_prior_snapshot:
            chain.append(rungs[i])
            i += 1

        snapshot_lines, snapshot = _collect_rung_snapshot_bindings(
            chain,
            ctx,
            indent=indent,
        )
        lines.extend(snapshot_lines)
        for rung in chain:
            lines.extend(
                compile_rung(
                    rung,
                    fn_name,
                    ctx,
                    indent=indent,
                    condition_snapshot=snapshot,
                )
            )
    return lines


def _compile_condition_group(
    conditions: list[Condition],
    ctx: CodegenContext,
    *,
    condition_snapshot: _ConditionSnapshotBindings | None = None,
) -> str:
    if not conditions:
        return "True"
    parts = [
        compile_condition(cond, ctx, condition_snapshot=condition_snapshot) for cond in conditions
    ]
    return " and ".join(parts)


def _calc_range_condition(value_var: str, dest_type: str, mode: str) -> str | None:
    if mode == "hex":
        return f"int({value_var}) < 0 or int({value_var}) > 0xFFFF"
    if dest_type == "INT":
        return f"int({value_var}) < -32768 or int({value_var}) > 32767"
    if dest_type == "DINT":
        return f"int({value_var}) < -2147483648 or int({value_var}) > 2147483647"
    if dest_type == "WORD":
        return f"int({value_var}) < 0 or int({value_var}) > 65535"
    return None


def _compile_calc_instruction(
    instr: CalcInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    ctx.mark_helper("_calc_math_isfinite")
    div_err = ctx.symbol_if_referenced(_FAULT_DIVISION_ERROR_TAG)
    value_expr = _compile_value(instr.expression, ctx)
    store_expr = _calc_store_expr("_calc_value", instr.dest.type.name, instr.mode, ctx)
    zero_store_expr = _calc_store_expr("0", instr.dest.type.name, instr.mode, ctx)
    div_err_line = f"    {div_err} = True" if div_err else "    pass"
    out_of_range_body = _compile_set_out_of_range_fault_body(ctx)
    enabled_body = [
        "try:",
        f"    _calc_value = {value_expr}",
        "except ZeroDivisionError:",
        div_err_line,
        "    _calc_value = 0",
        "except OverflowError:",
        *_indent_body(out_of_range_body, 4),
        "    _calc_value = 0",
        "if isinstance(_calc_value, float) and not math.isfinite(_calc_value):",
        div_err_line,
        "    _calc_value = 0",
    ]
    range_cond = _calc_range_condition("_calc_value", instr.dest.type.name, instr.mode)
    if range_cond and out_of_range_body != ["pass"]:
        enabled_body.append(f"if {range_cond}:")
        enabled_body.extend(f"    {line}" for line in out_of_range_body)
    enabled_body.append("try:")
    enabled_body.extend(_compile_assignment_lines(instr.dest, store_expr, ctx, indent=4))
    enabled_body.append("except OverflowError:")
    enabled_body.extend(_indent_body(out_of_range_body, 4))
    enabled_body.extend(_compile_assignment_lines(instr.dest, zero_store_expr, ctx, indent=4))
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
    disabled_children = _compile_instruction_list(instr.instructions, "False", ctx, indent=0)
    enabled_children = _compile_instruction_list(instr.instructions, "True", ctx, indent=0)
    body = [
        f"_iterations = max(1, int({count_expr}))",
        "for _for_i in range(_iterations):",
        *_indent_body(_compile_assignment_lines(instr.idx_tag, "_for_i", ctx, indent=0), 4),
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
