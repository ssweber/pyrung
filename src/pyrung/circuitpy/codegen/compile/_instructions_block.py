"""Automatically generated module split."""

from __future__ import annotations

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _INT_MAX,
)
from pyrung.circuitpy.codegen._util import (
    _indent_body,
    _range_reverse,
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
)
from pyrung.core.kernel import prove_effective_preset_key
from pyrung.core.tag import TagType

from ._core import _get_condition_snapshot, compile_condition
from ._primitives import (
    _compile_guarded_instruction,
    _compile_lvalue,
    _compile_range_setup,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _range_item_read_expr,
    _range_item_write_expr,
    _search_compare_expr,
    _timer_dt_to_units_expr,
)


def _compile_blockcopy_instruction(
    instr: BlockCopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    if instr.convert is not None:
        return _compile_blockcopy_converter_instruction(instr, enabled_expr, ctx, indent)
    stem = ctx.next_name("blockcopy")
    store_expr = _store_coerce_expr("_raw", _range_type_name(instr.dest), ctx)
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
        "_blockcopy_values = []",
        f"for _src_idx in {src_indices}:",
        f"    _raw = {_range_item_read_expr(instr.source, src_symbol, '_src_idx', ctx)}",
        "    _blockcopy_values.append(_raw)",
        f"for _raw, _dst_idx in zip(_blockcopy_values, {dst_indices}):",
        f"    {_range_item_write_expr(dst_symbol, '_dst_idx', store_expr)}",
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
    src_setup, src_symbol, src_indices, _ = _compile_range_setup(
        instr.source, ctx, stem=f"{stem}_src", include_addresses=False
    )
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    mode = converter.mode
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
        enabled_body.extend(
            [
                "try:",
                "    _converted = []",
                f"    for _src_idx in {src_indices}:",
                f"        _raw_char = _text_from_source_value({_range_item_read_expr(instr.source, src_symbol, '_src_idx', ctx)})",
                "        if len(_raw_char) != 1:",
                '            raise ValueError("BlockCopy text->numeric conversion requires single CHAR values")',
                f'        _numeric = _store_numeric_text_digit(_raw_char, "{mode}")',
                f"        _converted.append({_store_coerce_expr('_numeric', dst_type, ctx)})",
                f"    for _dst_idx, _converted_value in zip({dst_indices}, _converted):",
                f"        {_range_item_write_expr(dst_symbol, '_dst_idx', '_converted_value')}",
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
    stem = ctx.next_name("fill")
    store_expr = _store_coerce_expr("_fill_value", _range_type_name(instr.dest), ctx)
    dst_setup, dst_symbol, dst_indices, _ = _compile_range_setup(
        instr.dest, ctx, stem=f"{stem}_dst", include_addresses=False
    )
    value_expr = _compile_value(instr.value, ctx)
    enabled_body = [
        *dst_setup,
        f"_fill_value = {value_expr}",
        f"for _dst_idx in {dst_indices}:",
        f"    {_range_item_write_expr(dst_symbol, '_dst_idx', store_expr)}",
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
    compare_expr = _search_compare_expr(instr.condition, "_candidate", "_rhs")
    static_len = _static_range_length(instr.search_range)
    miss_body = [f"{result_write} = -1", f"{found_write} = False"]

    text_path = range_type == "CHAR"
    if text_path and instr.condition not in {"==", "!="}:
        raise ValueError("Text search only supports '==' and '!=' conditions")

    cursor_body: list[str] = []
    if instr.continuous:
        cursor_body.extend(
            [
                f"_current_result = int({result_read})",
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
            f"{_range_item_read_expr(instr.search_range, range_symbol, f'{range_indices}[_start + _off]', ctx)}) for _off in range(_window_len))",
            f"        if ({'(_candidate == _rhs)' if instr.condition == '==' else '(_candidate != _rhs)'}):",
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
    lines = [
        *range_setup,
        f"_clock_curr = {clock_expr}",
        f"_clock_prev = bool(_mem.get({key!r}, False))",
        "_rising_edge = _clock_curr and not _clock_prev",
        "if _rising_edge:",
        f"    _prev_values = [bool({_range_item_read_expr(instr.bit_range, range_symbol, '_idx', ctx)}) for _idx in {range_indices}]",
        f"    {_range_item_write_expr(range_symbol, f'{range_indices}[0]', f'bool({enabled_expr})')}",
        f"    for _pos in range(1, {shift_len_expr}):",
        f"        {_range_item_write_expr(range_symbol, f'{range_indices}[_pos]', '_prev_values[_pos - 1]')}",
        f"if {reset_expr}:",
        f"    for _idx in {range_indices}:",
        f"        {_range_item_write_expr(range_symbol, '_idx', 'False')}",
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

    step_read = _compile_value(instr.current_step, ctx)
    step_write = _compile_lvalue(instr.current_step, ctx)
    completion_write = _compile_lvalue(instr.completion_flag, ctx)
    output_symbols = [_compile_lvalue(tag, ctx) for tag in instr.outputs]
    pattern_literal = repr(tuple(tuple(bool(cell) for cell in row) for row in instr.pattern))
    step_count = len(instr.pattern)
    key_base = ctx.state_key_for(instr)
    event_prev_key = f"_drum_event_prev:{key_base}"
    event_ready_key = f"_drum_event_ready:{key_base}"
    last_step_key = f"_drum_last_step:{key_base}"
    jump_prev_key = f"_drum_jump_prev:{key_base}"
    jog_prev_key = f"_drum_jog_prev:{key_base}"

    snap_events = ctx._helper_condition_snapshots.get(id(instr), {}).get("events")
    if snap_events is not None:
        assert isinstance(snap_events, list)
        event_exprs = [f"bool({v})" for v in snap_events]
    else:
        event_exprs = [f"bool({compile_condition(cond, ctx)})" for cond in instr.events]
    reset_expr = _get_condition_snapshot(instr, "reset_condition", ctx) or compile_condition(
        instr.reset_condition, ctx
    )

    lines: list[str] = [
        f"_enabled = bool({enabled_expr})",
        f"_step_raw = int({step_read})",
        "_step = _step_raw",
        "_step_changed = False",
        f"if _enabled and ((_step < 1) or (_step > {step_count})):",
        "    _step = 1",
        f"    {step_write} = 1",
        "    _step_changed = True",
        f"elif (_step < 1) or (_step > {step_count}):",
        "    _step = 1",
    ]

    if instr.jump_condition is not None:
        jump_expr = _get_condition_snapshot(instr, "jump_condition", ctx) or compile_condition(
            instr.jump_condition, ctx
        )
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
        jog_expr = _get_condition_snapshot(instr, "jog_condition", ctx) or compile_condition(
            instr.jog_condition, ctx
        )
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
                f"        {step_write} = _step",
                "        _step_changed = True",
                "    else:",
                f"        {completion_write} = True",
            ],
            4,
        )
    )

    lines.extend(
        [
            "if _reset_active:",
            "    _step = 1",
            "    _step_changed = True",
            f"    {step_write} = 1",
            f"    {completion_write} = False",
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
                f"        {step_write} = _step",
            ]
        )

    if instr.jog_condition is not None:
        lines.extend(
            [
                f"if _enabled and _jog_edge and (_step < {step_count}):",
                "    _step += 1",
                "    _step_changed = True",
                f"    {step_write} = _step",
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

    step_read = _compile_value(instr.current_step, ctx)
    step_write = _compile_lvalue(instr.current_step, ctx)
    completion_write = _compile_lvalue(instr.completion_flag, ctx)
    accumulator_read = _compile_value(instr.accumulator, ctx)
    accumulator_write = _compile_lvalue(instr.accumulator, ctx)
    output_symbols = [_compile_lvalue(tag, ctx) for tag in instr.outputs]
    pattern_literal = repr(tuple(tuple(bool(cell) for cell in row) for row in instr.pattern))
    step_count = len(instr.pattern)
    key_base = ctx.state_key_for(instr)
    frac_key = f"_drum_time_frac:{key_base}"
    jump_prev_key = f"_drum_jump_prev:{key_base}"
    jog_prev_key = f"_drum_jog_prev:{key_base}"
    max_acc = _INT_MAX if instr.accumulator.type == TagType.INT else _DINT_MAX

    preset_exprs = [f"int({_compile_value(preset, ctx)})" for preset in instr.presets]
    reset_expr = _get_condition_snapshot(instr, "reset_condition", ctx) or compile_condition(
        instr.reset_condition, ctx
    )
    unit_expr = _timer_dt_to_units_expr(instr.unit, "_dt", "_frac")

    lines: list[str] = [
        f"_enabled = bool({enabled_expr})",
        f"_step_raw = int({step_read})",
        "_step = _step_raw",
        "_step_changed = False",
        "_reset_step_data = False",
        f"if _enabled and ((_step < 1) or (_step > {step_count})):",
        "    _step = 1",
        "    _step_changed = True",
        "    _reset_step_data = True",
        f"    {step_write} = 1",
        f"elif (_step < 1) or (_step > {step_count}):",
        "    _step = 1",
        f"_acc = int({accumulator_read})",
        f"_frac = float(_mem.get({frac_key!r}, 0.0))",
    ]

    if instr.jump_condition is not None:
        jump_expr = _get_condition_snapshot(instr, "jump_condition", ctx) or compile_condition(
            instr.jump_condition, ctx
        )
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
        jog_expr = _get_condition_snapshot(instr, "jog_condition", ctx) or compile_condition(
            instr.jog_condition, ctx
        )
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
                *(
                    [f'_mem["{prove_effective_preset_key(instr.completion_flag.name)}"] = _preset']
                    if ctx.proof_metadata
                    else []
                ),
                "if _acc >= _preset:",
                f"    if _step < {step_count}:",
                "        _step += 1",
                "        _step_changed = True",
                "        _reset_step_data = True",
                f"        {step_write} = _step",
                "    else:",
                f"        {completion_write} = True",
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
            f"    {step_write} = 1",
            f"    {completion_write} = False",
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
                f"        {step_write} = _step",
            ]
        )

    if instr.jog_condition is not None:
        lines.extend(
            [
                f"if _enabled and _jog_edge and (_step < {step_count}):",
                "    _step += 1",
                "    _step_changed = True",
                "    _reset_step_data = True",
                f"    {step_write} = _step",
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
            f"    {accumulator_write} = _acc",
            f"    _mem[{frac_key!r}] = _frac",
        ]
    )

    if instr.jump_condition is not None:
        lines.append(f"_mem[{jump_prev_key!r}] = _jump_curr")
    if instr.jog_condition is not None:
        lines.append(f"_mem[{jog_prev_key!r}] = _jog_curr")

    return [" " * indent + line for line in lines]
