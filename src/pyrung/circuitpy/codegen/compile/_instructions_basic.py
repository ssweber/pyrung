"""Automatically generated module split."""

from __future__ import annotations

from pyrung.circuitpy.codegen._constants import (
    _DINT_MAX,
    _DINT_MIN,
    _INT_MAX,
)
from pyrung.circuitpy.codegen._util import (
    _bool_literal,
    _coil_target_default,
    _indent_body,
    _optional_value_type_name,
    _subroutine_symbol,
    _value_type_name,
)
from pyrung.circuitpy.codegen.context import (
    CodegenContext,
)
from pyrung.core.copy_converters import CopyConverter
from pyrung.core.instruction import (
    CallInstruction,
    CopyInstruction,
    CountDownInstruction,
    CountUpInstruction,
    LatchInstruction,
    OffDelayInstruction,
    OnDelayInstruction,
    OutInstruction,
    ResetInstruction,
)

from ._core import compile_condition
from ._primitives import (
    _compile_assignment_lines,
    _compile_guarded_instruction,
    _compile_set_out_of_range_fault_body,
    _compile_target_write_lines,
    _compile_value,
    _copy_converter_target_info,
    _copy_converter_write_lines,
    _timer_dt_to_units_expr,
)


def _compile_latch_instruction(
    instr: LatchInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    lines = [f"{' ' * indent}if {enabled_expr}:"]
    lines.extend(_compile_target_write_lines(instr.target, "True", ctx, indent + 4))
    return lines


def _compile_reset_instruction(
    instr: ResetInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    default_expr = _coil_target_default(instr.target, ctx)
    lines = [f"{' ' * indent}if {enabled_expr}:"]
    lines.extend(_compile_target_write_lines(instr.target, default_expr, ctx, indent + 4))
    return lines


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
    if instr.convert is not None:
        return _compile_copy_converter_instruction(instr, enabled_expr, ctx, indent)
    target_type = _value_type_name(instr.target)
    ctx.mark_helper("_store_copy_value_to_type")
    source_expr = _compile_value(instr.source, ctx)
    value_expr = f'_store_copy_value_to_type({source_expr}, "{target_type}")'
    enabled_body = _compile_assignment_lines(instr.target, value_expr, ctx, indent=0)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _compile_copy_converter_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    converter = instr.convert
    if not isinstance(converter, CopyConverter):
        raise TypeError("copy converter compiler requires CopyConverter")

    stem = ctx.next_name("copymod")
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    mode = converter.mode
    source_expr = _compile_value(instr.source, ctx)

    setup, target_kind, target_symbol, target_start_var, target_type = _copy_converter_target_info(
        instr.target, ctx, stem
    )
    values_var = f"_{stem}_values"
    write_lines = _copy_converter_write_lines(
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
        source_type = _optional_value_type_name(instr.source)
        enabled_body.extend(
            [
                "try:",
                "    _rendered = _render_text_from_numeric(",
                f"        {source_expr},",
                f"        source_type={source_type!r},",
                f"        suppress_zero={converter.suppress_zero!r},",
                "        pad=None,",
                f"        exponential={converter.exponential!r},",
                "    )",
                f"    _rendered += _termination_char({converter.termination_code!r})",
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
