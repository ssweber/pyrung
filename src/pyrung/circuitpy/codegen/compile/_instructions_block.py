"""Automatically generated module split."""

from __future__ import annotations

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _INT_MAX,
)
from pyrung.circuitpy.codegen._util import (
    _indent_body,
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
from pyrung.core.kernel import prove_effective_preset_key
from pyrung.core.tag import TagType

from ._core import _get_condition_snapshot, compile_condition
from ._primitives import (
    _compile_guarded_instruction,
    _compile_lvalue,
    _compile_range_flush,
    _compile_range_setup,
    _compile_set_out_of_range_fault_body,
    _compile_value,
    _copy_clamp_condition,
    _range_item_read_expr,
    _range_item_write_expr,
    _search_compare_expr,
    _timer_carry_lines,
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
    clamp_cond = _copy_clamp_condition(
        "_raw", _range_type_name(instr.dest), _range_type_name(instr.source)
    )
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    clamp_lines = (
        [f"    if {clamp_cond}:", *[f"        {line}" for line in fault_body]]
        if clamp_cond is not None and fault_body != ["pass"]
        else []
    )
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
        *clamp_lines,
        *_compile_range_flush(instr.dest, dst_symbol, ctx),
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
                *_indent_body(_compile_range_flush(instr.dest, dst_symbol, ctx), 4),
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
        *_compile_range_flush(instr.dest, dst_symbol, ctx),
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
        cursor_body.extend(
            [
                "    _cursor_index = None",
                f"    for _idx, _addr in enumerate({range_addrs}):",
                "        if _addr == _current_result:",
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
    # A dynamic window wholly in a sparse block's gap: refused every scan, clock or not.
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
    range_flush = _compile_range_flush(instr.bit_range, range_symbol, ctx)
    if range_flush:
        lines.extend(
            [
                f"if _rising_edge or bool({reset_expr}):",
                *_indent_body(range_flush, 4),
            ]
        )
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


def _compile_drum_instruction(
    instr: EventDrumInstruction | TimeDrumInstruction | TimeEventDrumInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    """One scan of a drum, the routine of ``_DrumBaseInstruction.execute`` for every kind.

    A kind supplies its step's end (``_drum_end_lines``) and what it remembers
    of every scan; a pin a drum does not have generates nothing.
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
            f"_mem[{f'_frac:{instr.accumulator.name}'!r}] = 0",
        ]
    complete = [f"{flag_write} = True", *(f"{symbol} = False" for symbol in output_symbols)]

    # The pins, read before the drum writes anything.
    lines: list[str] = [
        f"_enabled = bool({enabled_expr})",
        f"_step_raw = int({_compile_value(instr.current_step, ctx)})",
        "_step = _step_raw",
        f"_invalid = (_step < 1) or (_step > {step_count})",
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
                f"_jump_lands = _jump_curr and (not bool(_mem.get({jump_prev_key!r}, False)))",
                "if _jump_lands:",
                f"    _jump_target = int({_compile_value(instr.jump_step, ctx)})",
                f"    _jump_lands = 1 <= _jump_target <= {step_count}",
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
                f"_jog_edge = _jog_curr and (not bool(_mem.get({jog_prev_key!r}, False)))",
            ]
        )
    reset_expr = _get_condition_snapshot(instr, "reset_condition", ctx) or compile_condition(
        instr.reset_condition, ctx
    )
    lines.append(f"_reset_active = bool({reset_expr})")

    # A pin acts; the guard corrects a bad step unless a jog edge froze the scan.
    pin_branches: list[str] = []
    if has_jump:
        pin_branches.extend(
            [
                "if _jump_lands:",
                "    _step = _jump_target",
                "    _moved = True",
                "    _write_row = not _flag",
            ]
        )
    if has_jog:
        pin_branches.extend(
            [
                "elif _jog_edge:" if pin_branches else "if _jog_edge:",
                f"    if _step < {step_count}:",
                "        _step += 1",
                "        _moved = True",
                "        _write_row = True",
                "    elif not _flag:",
                *_indent_body(complete, 8),
                "        _flag = True",
            ]
        )
    pin_branches.extend(
        [
            "elif _invalid and _enabled:" if pin_branches else "if _invalid and _enabled:",
            f"    {step_write} = 1",
        ]
    )

    taken: list[str] = [
        f"_flag = bool({_compile_value(instr.completion_flag, ctx)})",
        "_write_row = False",
    ]
    if has_jump or has_jog:
        taken.append("_moved = False")
    taken.extend(pin_branches)
    if has_jump or has_jog:
        taken.extend(["if _moved:", f"    {step_write} = _step", *_indent_body(clear_time, 4)])

    # Own logic: rung true and the flag off.  A return scan writes the row and no more.
    taken.extend(
        [
            "if _enabled and (not _flag):",
            f"    if not bool(_mem.get({rung_prev_key!r}, False)):",
            "        _write_row = True",
            "    elif not _moved:" if has_jump or has_jog else "    else:",
            *_indent_body(_drum_end_lines(instr, ctx), 8),
            "        if _end_met:",
            f"            if _step < {step_count}:",
            "                _step += 1",
            f"                {step_write} = _step",
            *_indent_body(clear_time, 16),
            "                _write_row = True",
            "            else:",
            *_indent_body(complete, 16),
            "if _enabled and _write_row:",
            f"    _row = {pattern_literal}[_step - 1]",
            *(f"    {symbol} = bool(_row[{idx}])" for idx, symbol in enumerate(output_symbols)),
        ]
    )

    # Reset is a level and wins.
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
    if not has_jog:
        lines.append("else:")
    elif has_jump:
        lines.append("elif _jump_lands or not (_invalid and _jog_edge):")
    else:
        lines.append("elif not (_invalid and _jog_edge):")
    lines.extend(_indent_body(taken, 4))

    if isinstance(instr, EventDrumInstruction):
        lines.extend(
            [
                *_compile_step_selector_lines(
                    step_var="_step",
                    target_var="_event_curr",
                    value_exprs=_event_drum_event_exprs(instr, ctx),
                ),
                f"_mem[{f'_drum_event_prev:{key_base}'!r}] = _event_curr",
                f"_mem[{f'_drum_last_step:{key_base}'!r}] = _step",
            ]
        )
    lines.append(f"_mem[{rung_prev_key!r}] = _enabled and (not _reset_active)")
    if has_jump:
        lines.append(f"_mem[{jump_prev_key!r}] = _jump_curr")
    if has_jog:
        lines.append(f"_mem[{jog_prev_key!r}] = _jog_curr")

    return [" " * indent + line for line in lines]


def _drum_end_lines(
    instr: EventDrumInstruction | TimeDrumInstruction | TimeEventDrumInstruction,
    ctx: CodegenContext,
) -> list[str]:
    """Own logic off the return scan: tick if the step has a time, and set ``_end_met``."""
    if isinstance(instr, EventDrumInstruction):
        key_base = ctx.state_key_for(instr)
        return [
            *_compile_step_selector_lines(
                step_var="_step",
                target_var="_event_curr",
                value_exprs=_event_drum_event_exprs(instr, ctx),
            ),
            # A step reached by a write to the register or a correction has spent its edge.
            f"_end_met = _event_curr and (_step_raw == int(_mem.get({f'_drum_last_step:{key_base}'!r}, 0)))"
            f" and (not bool(_mem.get({f'_drum_event_prev:{key_base}'!r}, False)))",
        ]

    preset_exprs = [
        None if preset is None else f"int({_compile_value(preset, ctx)})"
        for preset in instr.presets
    ]
    tick = _drum_tick_lines(instr, ctx)
    if isinstance(instr, TimeDrumInstruction):
        lines = [
            *_compile_step_selector_lines(
                step_var="_step",
                target_var="_limit",
                value_exprs=[expr for expr in preset_exprs if expr is not None],
            ),
            *tick,
        ]
        if ctx.proof_metadata:
            preset_key = prove_effective_preset_key(instr.completion_flag.name)
            lines.append(f"_mem[{preset_key!r}] = _limit")
        lines.append("_end_met = _acc >= _limit")
        return lines

    lines = []
    timed_steps = sum(expr is not None for expr in preset_exprs)
    if timed_steps == len(preset_exprs):
        lines.extend(
            _compile_step_selector_lines(
                step_var="_step",
                target_var="_limit",
                value_exprs=[expr for expr in preset_exprs if expr is not None],
            )
        )
        lines.extend(tick)
    elif timed_steps:
        lines.extend(
            [
                "_limit = None",
                *_compile_step_selector_lines(
                    step_var="_step",
                    target_var="_limit",
                    value_exprs=[expr if expr is not None else "None" for expr in preset_exprs],
                ),
                "if _limit is not None:",
                *_indent_body(tick, 4),
            ]
        )
    lines.extend(
        _compile_step_selector_lines(
            step_var="_step",
            target_var="_end_met",
            value_exprs=_time_event_end_exprs(instr, preset_exprs, ctx),
        )
    )
    return lines


def _drum_tick_lines(
    instr: TimeDrumInstruction | TimeEventDrumInstruction, ctx: CodegenContext
) -> list[str]:
    """Add this scan's time to Acc; the step's preset, ``_limit``, is the tick's limit."""
    acc_write = _compile_lvalue(instr.accumulator, ctx)
    lines = [f"_acc = int({_compile_value(instr.accumulator, ctx)})"]
    if ctx.abstract_timers:
        lines.append(f"_acc = _bb_tick(_mem, {instr.accumulator.name!r}, _acc, _limit)")
    else:
        frac_key = f"_frac:{instr.accumulator.name}"
        max_acc = _INT_MAX if instr.accumulator.type == TagType.INT else _DINT_MAX
        lines.extend(
            [
                *_timer_carry_lines(instr.accumulator, instr.unit, ctx, ""),
                f"_mem[{frac_key!r}] = _new_frac",
                f"_acc = min(_acc + _int_units, {max_acc})",
            ]
        )
    lines.append(f"{acc_write} = _acc")
    return lines


def _event_drum_event_exprs(instr: EventDrumInstruction, ctx: CodegenContext) -> list[str]:
    snap_events = ctx._helper_condition_snapshots.get(id(instr), {}).get("events")
    if snap_events is not None:
        assert isinstance(snap_events, list)
        return [f"bool({v})" for v in snap_events]
    return [f"bool({compile_condition(cond, ctx)})" for cond in instr.events]


def _time_event_end_exprs(
    instr: TimeEventDrumInstruction,
    preset_exprs: list[str | None],
    ctx: CodegenContext,
) -> list[str]:
    """Per step: its time is up (if it has a preset) and its event is on (if it has one)."""
    snap_events = ctx._helper_condition_snapshots.get(id(instr), {}).get("events")
    # Snapshots exist for the steps that have an event, in step order.
    snapshots = iter(snap_events) if isinstance(snap_events, list) else None
    exprs: list[str] = []
    for preset_expr, event in zip(preset_exprs, instr.events, strict=True):
        parts: list[str] = []
        if preset_expr is not None:
            parts.append("(_acc >= _limit)")
        if event is not None:
            if snapshots is not None:
                parts.append(f"bool({next(snapshots)})")
            else:
                parts.append(f"bool({compile_condition(event, ctx)})")
        exprs.append(" and ".join(parts))
    return exprs
