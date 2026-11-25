"""Automatically generated module split."""

from __future__ import annotations

from typing import assert_never

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _INT_MAX,
)
from pyrung.circuitpy.codegen._util import (
    _copy_store_expr,
    _indent_body,
    _optional_value_type_name,
    _range_type_name,
    _static_range_length,
    _store_coerce_expr,
)
from pyrung.circuitpy.codegen.context import (
    CodegenContext,
)
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.instruction import (
    BlockCopyInstruction,
    EventDrumInstruction,
    FillInstruction,
    SearchInstruction,
    ShiftInstruction,
    TimeDrumInstruction,
    TimeEventDrumInstruction,
)
from pyrung.core.instruction.conversions import copy_source_kind
from pyrung.core.instruction.operands import (
    ConstArg,
    ExprArg,
    PointerArg,
    PointerRangeArg,
    RangeArg,
    Span,
    TagArg,
)
from pyrung.core.kernel import prove_effective_preset_key
from pyrung.core.memory_block import BlockRange, IndirectBlockRange
from pyrung.core.tag import TagType

from ._core import _get_condition_snapshot, compile_condition
from ._emit import (
    emit_choose,
    emit_cmp,
    emit_frac,
    emit_mem_get,
    emit_mem_set,
    emit_prev,
    emit_reached,
    emit_rise,
    emit_tick,
    emit_truth,
)
from ._primitives import (
    _compile_guarded_instruction,
    _compile_lvalue,
    _compile_range_flush,
    _compile_range_setup,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _copy_clamp_condition,
    _number_text_lines,
    _range_item_read_expr,
    _range_item_write_expr,
    _refuse,
)


def _copy_range(span: Span) -> BlockRange | IndirectBlockRange:
    """Return the range that *span* holds, as the shared range helpers take it.

    The generated code reads an ``immediate()`` range as it reads the plain range.
    """
    match span:
        case RangeArg(span=fixed):
            return fixed
        case PointerRangeArg(span=pointed):
            return pointed
        case _:
            assert_never(span)


def _compile_blockcopy_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if instr.convert is not None:
        return _compile_blockcopy_converter_instruction(instr, enabled_expr, ctx, indent)
    stem = ctx.next_name("blockcopy")
    source, dest = _copy_range(instr.source), _copy_range(instr.dest)
    store_expr = _copy_store_expr(
        "_raw", _range_type_name(dest), TagType[_range_type_name(source)], ctx
    )
    clamp_cond = _copy_clamp_condition("_raw", _range_type_name(dest), _range_type_name(source))
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    clamp_lines = (
        [f"    if {clamp_cond}:", *[f"        {line}" for line in fault_body]]
        if clamp_cond is not None and fault_body != ["pass"]
        else []
    )
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    # Stepping resolves the destination range before the source range.
    enabled_body = [
        *dst_setup,
        *src_setup,
        f"if len({src_indices}) != len({dst_indices}):",
        f'    raise ValueError(f"blockcopy() needs ranges of the same length. The source has {{len({src_indices})}} tags, and the destination has {{len({dst_indices})}}.")',
        "_blockcopy_values = []",
        f"for _src_idx in {src_indices}:",
        f"    _raw = {_range_item_read_expr(source, src_symbol, '_src_idx', ctx)}",
        "    _blockcopy_values.append(_raw)",
        f"for _raw, _dst_idx in zip(_blockcopy_values, {dst_indices}):",
        f"    {_range_item_write_expr(dst_symbol, '_dst_idx', store_expr)}",
        *clamp_lines,
        *_compile_range_flush(dest, dst_symbol, ctx),
    ]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_blockcopy_converter_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    converter = instr.convert
    if not isinstance(converter, CopyConverter):
        raise TypeError("blockcopy converter compiler requires CopyConverter")

    stem = ctx.next_name("blockcopymod")
    source, dest = _copy_range(instr.source), _copy_range(instr.dest)
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    mode = converter.mode
    dst_type = _range_type_name(dest)
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    # Stepping resolves the destination range before the source range.
    enabled_body: list[str] = [
        *dst_setup,
        *src_setup,
        f"if len({src_indices}) != len({dst_indices}):",
        f'    raise ValueError(f"blockcopy() needs ranges of the same length. The source has {{len({src_indices})}} tags, and the destination has {{len({dst_indices})}}.")',
    ]

    if mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        # A character that does not convert leaves its own element and faults.
        enabled_body.extend(
            [
                f"for _src_idx, _dst_idx in zip({src_indices}, {dst_indices}):",
                "    try:",
                f"        _raw_char = _text_from_source_value({_range_item_read_expr(source, src_symbol, '_src_idx', ctx)})",
                "        if len(_raw_char) != 1:",
                '            raise ValueError("BlockCopy text->numeric conversion requires single CHAR values")',
                f'        _numeric = _store_numeric_text_digit(_raw_char, "{mode}")',
                f"        {_range_item_write_expr(dst_symbol, '_dst_idx', _store_coerce_expr('_numeric', dst_type, ctx))}",
                "    except (IndexError, TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 8),
                *_compile_range_flush(dest, dst_symbol, ctx),
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
    stem = ctx.next_name("fill")
    dest = _copy_range(instr.dest)
    dest_type = _range_type_name(dest)
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    value = instr.value
    match value:
        case TagArg(tag=tag):
            value_expr = _compile_value(tag, ctx)
        case PointerArg(ref=ref):
            value_expr = _compile_value(ref, ctx)
        case ExprArg(expr=expr):
            value_expr = _compile_value(expr, ctx)
            if ctx.kernel_runtime and dest_type == "REAL":
                ctx.mark_helper("_computed_real")
                value_expr = f"_computed_real({value_expr})"
        case ConstArg(value=constant):
            value_expr = _compile_value(constant, ctx)
        case _:
            assert_never(value)
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    converter = instr.convert
    if converter is not None:
        # A number into a TXT range writes its text, as the core's FillInstruction does.
        ctx.mark_helper("_as_single_ascii_char")
        text_lines = _number_text_lines("_fill_text", "_fill_value", value, converter, ctx)
        enabled_body = [
            *dst_setup,
            f"_fill_value = {value_expr}",
            "try:",
            *_indent_body(text_lines, 4),
            "    _fill_char = _as_single_ascii_char(_fill_text)",
            "except (TypeError, ValueError, OverflowError):",
            *_indent_body(fault_body, 4),
            "else:",
            f"    for _dst_idx in {dst_indices}:",
            f"        {_range_item_write_expr(dst_symbol, '_dst_idx', '_fill_char')}",
            *_indent_body(_compile_range_flush(dest, dst_symbol, ctx), 4),
        ]
        return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
    source_type = (
        "CHAR"
        if isinstance(value, ConstArg) and isinstance(value.value, str)
        else _optional_value_type_name(value)
    )
    store_expr = _copy_store_expr("_fill_value", dest_type, copy_source_kind(value), ctx)
    clamp_cond = _copy_clamp_condition("_fill_value", dest_type, source_type)
    clamp_lines = (
        [f"if {dst_indices} and ({clamp_cond}):", *_indent_body(fault_body, 4)]
        if clamp_cond is not None and fault_body != ["pass"]
        else []
    )
    enabled_body = [
        *dst_setup,
        f"_fill_value = {value_expr}",
        f"for _dst_idx in {dst_indices}:",
        f"    {_range_item_write_expr(dst_symbol, '_dst_idx', store_expr)}",
        *_compile_range_flush(dest, dst_symbol, ctx),
        *clamp_lines,
    ]
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
    result_read = _compile_value(instr.result, ctx)
    result_write = _compile_lvalue(instr.result, ctx)
    found_write = _compile_lvalue(instr.found, ctx)
    value_expr = _compile_value(instr.value, ctx)
    range_type = _range_type_name(instr.search_range)
    compare_expr = emit_cmp("_candidate", instr.condition, "_rhs")
    static_len = _static_range_length(instr.search_range)
    miss_body = [f"{result_write} = -1", f"{found_write} = False"]

    text_path = range_type == "CHAR"
    if text_path and instr.condition not in {"==", "!="}:
        _refuse(ctx, ValueError, "Text search only supports '==' and '!=' conditions")

    cursor_body: list[str] = []
    if instr.continuous:
        cursor_body.extend(
            [
                f"_current_result = int({result_read})",
                f"if {emit_cmp('_current_result', '==', '0', paren=False)}:",
                "    _cursor_index = 0",
                f"elif {emit_cmp('_current_result', '==', '-1', paren=False)}:",
                "    _cursor_index = None",
                "else:",
            ]
        )
        cursor_body.extend(
            [
                "    _cursor_index = None",
                f"    for _idx, _addr in enumerate({range_addrs}):",
                f"        if {emit_cmp('_addr', '==', '_current_result', paren=False)}:",
                f"            if _idx + 1 < len({range_addrs}):",
                "                _cursor_index = _idx + 1",
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
            _refuse(ctx, ValueError, "Text search value cannot be empty")
        fixed_window_len = len(instr.value) if isinstance(instr.value, str) else None
        len_expr = str(static_len) if static_len is not None else f"len({range_indices})"
        match_lines = [
            "if _cursor_index > _last_start:",
            *[f"    {line}" for line in miss_body],
            "else:",
            "    _matched = None",
            "    for _start in range(_cursor_index, _last_start + 1):",
            "        _candidate = ''.join(str("
            f"{_range_item_read_expr(instr.search_range, range_symbol, f'{range_indices}[_start + _off]', ctx)}) for _off in range(_window_len))",
            f"        if ({compare_expr}):",
            "            _matched = _start",
            "            break",
            "    if _matched is None:",
            *[f"        {line}" for line in miss_body],
            "    else:",
            f"        {result_write} = {range_addrs}[_matched]",
            f"        {found_write} = True",
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
                f"        _candidate = {_range_item_read_expr(instr.search_range, range_symbol, f'{range_indices}[_idx]', ctx)}",
                f"        if {compare_expr}:",
                "            _matched = _idx",
                "            break",
                "    if _matched is None:",
                *[f"        {line}" for line in miss_body],
                "    else:",
                f"        {result_write} = {range_addrs}[_matched]",
                f"        {found_write} = True",
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
    clock_expr = _get_condition_snapshot(instr, "clock_condition", ctx) or compile_condition(
        instr.clock_condition, ctx
    )
    reset_expr = _get_condition_snapshot(instr, "reset_condition", ctx) or compile_condition(
        instr.reset_condition, ctx
    )
    # A dynamic window wholly in a sparse block's gap is refused every scan, clock or not.
    empty_check = (
        [
            f"if not {range_indices}:",
            '    raise ValueError("shift bit_range resolved to an empty range")',
        ]
        if static_len is None
        else []
    )
    lines = [
        *range_setup,
        *empty_check,
        f"_clock_curr = {clock_expr}",
        f"_clock_prev = {emit_prev(key)}",
        "_rising_edge = _clock_curr and not _clock_prev",
        "if _rising_edge:",
        f"    _prev_values = [{emit_truth(_range_item_read_expr(instr.bit_range, range_symbol, '_idx', ctx))} for _idx in {range_indices}]",
        f"    {_range_item_write_expr(range_symbol, f'{range_indices}[0]', f'bool({enabled_expr})')}",
        f"    for _pos in range(1, {shift_len_expr}):",
        f"        {_range_item_write_expr(range_symbol, f'{range_indices}[_pos]', '_prev_values[_pos - 1]')}",
        f"if {reset_expr}:",
        f"    for _idx in {range_indices}:",
        f"        {_range_item_write_expr(range_symbol, '_idx', 'False')}",
        emit_mem_set(key, "_clock_curr"),
    ]
    range_flush = _compile_range_flush(instr.bit_range, range_symbol, ctx)
    if range_flush:
        lines.extend(
            [
                f"if _rising_edge or bool({reset_expr}):",
                *_indent_body(range_flush, 4),
            ]
        )
    return [" " * indent + line for line in lines]


def _compile_drum_instruction(
    instr: EventDrumInstruction | TimeDrumInstruction | TimeEventDrumInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    """Generate one scan of a drum, the routine of ``_DrumBaseInstruction.execute`` for every kind.

    A kind supplies its step's end (``_drum_end_lines``) and what it remembers
    of every scan.  A pin a drum does not have generates nothing.
    """
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")

    step_write = _compile_lvalue(instr.current_step, ctx)
    flag_write = _compile_lvalue(instr.completion_flag, ctx)
    output_symbols = [_compile_lvalue(tag, ctx) for tag in instr.outputs]
    pattern_literal = repr(tuple(tuple(bool(cell) for cell in row) for row in instr.pattern))
    step_count = len(instr.pattern)
    key_base = ctx.state_key_for(instr)
    jump_prev_key = f"_drum_jump_prev:{key_base}"
    jog_prev_key = f"_drum_jog_prev:{key_base}"
    rung_prev_key = f"_drum_rung_prev:{key_base}"
    has_jump = instr.jump_condition is not None and instr.jump_step is not None
    has_jog = instr.jog_condition is not None

    clear_time: list[str] = []
    if not isinstance(instr, EventDrumInstruction):
        clear_time = [
            f"{_compile_lvalue(instr.accumulator, ctx)} = 0",
            emit_frac(instr.accumulator, "0", quote="repr"),
        ]
    complete = [f"{flag_write} = True", *(f"{symbol} = False" for symbol in output_symbols)]

    # The pins, read before the drum writes anything.
    lines: list[str] = [
        f"_enabled = bool({enabled_expr})",
        f"_step_raw = int({_compile_value(instr.current_step, ctx)})",
        "_step = _step_raw",
        f"_invalid = {emit_cmp('_step', '<', '1')} or {emit_cmp('_step', '>', str(step_count))}",
        "if _invalid:",
        "    _step = 1",
    ]
    if has_jump:
        assert instr.jump_condition is not None
        jump_expr = _get_condition_snapshot(instr, "jump_condition", ctx) or compile_condition(
            instr.jump_condition, ctx
        )
        lines.extend(
            [
                f"_jump_curr = bool({jump_expr})",
                f"_jump_edge = {emit_rise('_jump_curr', jump_prev_key, paren=True)}",
                "if _jump_edge:",
                f"    _jump_target = int({_compile_value(instr.jump_step, ctx)})",
            ]
        )
    if has_jog:
        assert instr.jog_condition is not None
        jog_expr = _get_condition_snapshot(instr, "jog_condition", ctx) or compile_condition(
            instr.jog_condition, ctx
        )
        lines.extend(
            [
                f"_jog_curr = bool({jog_expr})",
                f"_jog_edge = {emit_rise('_jog_curr', jog_prev_key, paren=True)}",
            ]
        )
    reset_expr = _get_condition_snapshot(instr, "reset_condition", ctx) or compile_condition(
        instr.reset_condition, ctx
    )
    lines.append(f"_reset_active = bool({reset_expr})")

    flag_on = emit_truth(_compile_value(instr.completion_flag, ctx))
    write_row = [
        f"_row = {pattern_literal}[_step - 1]",
        *(f"{symbol} = bool(_row[{idx}])" for idx, symbol in enumerate(output_symbols)),
    ]
    move = [f"{step_write} = _step", *clear_time]
    not_last = emit_cmp("_step", "<", str(step_count), paren=False)

    # Each scan takes one action, chosen by priority: reset (a level), a jump edge, a jog
    # edge, own logic.
    lines.extend(
        [
            "if _reset_active:",
            "    _step = 1",
            f"    {step_write} = 1",
            f"    {flag_write} = False",
            *(f"    {symbol} = False" for symbol in output_symbols),
            *_indent_body(clear_time, 4),
        ]
    )
    if has_jump:
        # A jump to a step the drum does not have does nothing, and the scan is taken.
        lines.extend(
            [
                "elif _jump_edge:",
                f"    if 1 <= _jump_target <= {step_count}:",
                "        _step = _jump_target",
                *_indent_body(move, 8),
                f"        if _enabled and (not {flag_on}):",
                *_indent_body(write_row, 12),
            ]
        )
    if has_jog:
        # A jog on a bad step does nothing.
        lines.extend(
            [
                "elif _jog_edge:",
                "    if not _invalid:",
                f"        if {not_last}:",
                "            _step += 1",
                *_indent_body(move, 12),
                "            if _enabled:",
                *_indent_body(write_row, 16),
                f"        elif not {flag_on}:",
                *_indent_body(complete, 12),
            ]
        )
    # Own logic with the rung true: a correction comes first (no row, and it takes no
    # scan).  Then, with the flag off, comes the return scan's row, or the step's time
    # or event.
    lines.extend(
        [
            "elif _enabled:",
            "    if _invalid:",
            f"        {step_write} = 1",
            f"    if not {flag_on}:",
            f"        if not {emit_prev(rung_prev_key)}:",
            *_indent_body(write_row, 12),
            "        else:",
            *_indent_body(_drum_end_lines(instr, ctx), 12),
            "            if _end_met:",
            f"                if {not_last}:",
            "                    _step += 1",
            *_indent_body(move, 20),
            *_indent_body(write_row, 20),
            "                else:",
            *_indent_body(complete, 20),
        ]
    )

    if isinstance(instr, EventDrumInstruction):
        lines.extend(
            [
                *emit_choose("_step", "_event_curr", _event_drum_event_exprs(instr, ctx)),
                emit_mem_set(f"_drum_event_prev:{key_base}", "_event_curr"),
                emit_mem_set(f"_drum_last_step:{key_base}", "_step"),
            ]
        )
    lines.append(emit_mem_set(rung_prev_key, "_enabled and (not _reset_active)"))
    if has_jump:
        lines.append(emit_mem_set(jump_prev_key, "_jump_curr"))
    if has_jog:
        lines.append(emit_mem_set(jog_prev_key, "_jog_curr"))

    return [" " * indent + line for line in lines]


def _drum_end_lines(
    instr: EventDrumInstruction | TimeDrumInstruction | TimeEventDrumInstruction,
    ctx: CodegenContext,
) -> list[str]:
    """Generate own logic off the return scan: tick if the step has a time, and set ``_end_met``."""
    if isinstance(instr, EventDrumInstruction):
        key_base = ctx.state_key_for(instr)
        last_step = f"int({emit_mem_get(f'_drum_last_step:{key_base}', '0', quote='repr')})"
        return [
            *emit_choose("_step", "_event_curr", _event_drum_event_exprs(instr, ctx)),
            # A step reached by a write to the register, or by a correction, has spent its
            # edge.
            f"_end_met = _event_curr and {emit_cmp('_step_raw', '==', last_step)}"
            f" and (not {emit_prev(f'_drum_event_prev:{key_base}')})",
        ]

    preset_exprs = [
        None if preset is None else f"int({_compile_value(preset, ctx)})"
        for preset in instr.presets
    ]
    tick = _drum_tick_lines(instr, ctx)
    if isinstance(instr, TimeDrumInstruction):
        lines = [
            *emit_choose("_step", "_limit", [expr for expr in preset_exprs if expr is not None]),
            *tick,
        ]
        if ctx.proof_metadata:
            preset_key = prove_effective_preset_key(instr.completion_flag.name)
            lines.append(emit_mem_set(preset_key, "_limit"))
        lines.append(f"_end_met = {emit_reached('_acc', '_limit')}")
        return lines

    lines = []
    timed_steps = sum(expr is not None for expr in preset_exprs)
    if timed_steps == len(preset_exprs):
        lines.extend(
            emit_choose("_step", "_limit", [expr for expr in preset_exprs if expr is not None])
        )
        lines.extend(tick)
    elif timed_steps:
        lines.extend(
            [
                "_limit = None",
                *emit_choose(
                    "_step",
                    "_limit",
                    [expr if expr is not None else "None" for expr in preset_exprs],
                ),
                "if _limit is not None:",
                *_indent_body(tick, 4),
            ]
        )
    return [*lines, *emit_choose("_step", "_end_met", _time_event_end_exprs(instr, ctx))]


def _drum_tick_lines(
    instr: TimeDrumInstruction | TimeEventDrumInstruction, ctx: CodegenContext
) -> list[str]:
    """Add this scan's time to Acc; the step's preset, ``_limit``, is the tick's limit."""
    acc_write = _compile_lvalue(instr.accumulator, ctx)
    acc_read = _compile_value(instr.accumulator, ctx)
    max_acc = _INT_MAX if instr.accumulator.type == TagType.INT else _DINT_MAX
    tick = emit_tick(
        instr.accumulator, instr.unit, ctx, acc_read=acc_read, max_acc=max_acc, frac_first=True
    )
    return [*tick, f"{acc_write} = _acc"]


def _event_drum_event_exprs(instr: EventDrumInstruction, ctx: CodegenContext) -> list[str]:
    snap_events = ctx._helper_condition_snapshots.get(id(instr), {}).get("events")
    if snap_events is not None:
        assert isinstance(snap_events, list)
        return [f"bool({v})" for v in snap_events]
    return [f"bool({compile_condition(cond, ctx)})" for cond in instr.events]


def _time_event_end_exprs(
    instr: TimeEventDrumInstruction,
    ctx: CodegenContext,
) -> list[str]:
    """Return, per step, whether its time is up (if it has a preset) and its event is on."""
    snap_events = ctx._helper_condition_snapshots.get(id(instr), {}).get("events")
    # Snapshots exist only for the steps that have an event, in step order.
    snapshots = iter(snap_events) if isinstance(snap_events, list) else None
    exprs: list[str] = []
    for preset, event in zip(instr.presets, instr.events, strict=True):
        parts: list[str] = []
        if preset is not None:
            parts.append(f"({emit_reached('_acc', '_limit')})")
        if event is not None:
            if snapshots is not None:
                parts.append(f"bool({next(snapshots)})")
            else:
                parts.append(f"bool({compile_condition(event, ctx)})")
        exprs.append(" and ".join(parts))
    return exprs
