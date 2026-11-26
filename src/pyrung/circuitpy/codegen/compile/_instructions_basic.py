"""Automatically generated module split."""

from __future__ import annotations

from typing import Any, assert_never

from pyrung.circuitpy.codegen._util import (
    _bool_literal,
    _coil_reset_value,
    _copy_store_expr,
    _indent_body,
    _optional_value_type_name,
    _store_coerce_expr,
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
from pyrung.core.instruction.conversions import (
    BINARY_BITS,
    BINARY_VALUE,
    copy_binary_store,
    copy_source_kind,
)
from pyrung.core.instruction.counters import COUNT_DOWN_MEMORY, COUNT_UP_MEMORY
from pyrung.core.instruction.operands import (
    ConstArg,
    Dest,
    ExprArg,
    PointerArg,
    TagArg,
    Value,
)
from pyrung.core.instruction.timers import tof_armed_key
from pyrung.core.kernel import prove_effective_preset_key
from pyrung.core.memory_block import IndirectExprRef, IndirectRef, block_room, element_names_after
from pyrung.core.relations.records import Const, TagValue
from pyrung.core.tag import Tag, TagType

from ._core import _get_condition_snapshot, compile_condition
from ._emit import (
    emit_acc_rest,
    emit_cmp,
    emit_count,
    emit_done,
    emit_if_enabled,
    emit_mem_get,
    emit_mem_set,
    emit_oneshot,
    emit_rise,
    emit_store,
    emit_tick,
    emit_truth,
)
from ._primitives import (
    _compile_assignment_lines,
    _compile_copy_source,
    _compile_guarded_instruction,
    _compile_lvalue,
    _compile_set_address_error_fault_body,
    _compile_set_out_of_range_fault_body,
    _compile_target_write_lines,
    _compile_value,
    _compute_sequential_reloads,
    _copy_clamp_condition,
    _copy_converter_target_info,
    _copy_converter_write_lines,
    _copy_source_past_end_test,
    _number_text_lines,
    _pack_store_expr,
    _past_end_keeps,
    _pointer_check_lines,
)


def _compile_status_bit_lines(
    instr: OnDelayInstruction | OffDelayInstruction | CountUpInstruction | CountDownInstruction,
    ctx: CodegenContext,
    indent: int,
    **values: str,
) -> list[str]:
    """Emit assignment lines for optional status bits (EN/TT or CU/CD)."""
    lines: list[str] = []
    sp = " " * indent
    for attr, val_expr in values.items():
        tag = getattr(instr, attr, None)
        if tag is not None:
            lvalue = _compile_lvalue(tag, ctx)
            lines.append(f"{sp}{emit_store(lvalue, val_expr, ctx=ctx, dst=tag.name, term=None)}")
    return lines


def _compile_latch_instruction(
    instr: LatchInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    writes = _compile_target_write_lines(instr.target, "True", ctx, 0)
    return [f"{' ' * indent}{line}" for line in emit_if_enabled(enabled_expr, writes, fold=False)]


def _compile_reset_instruction(
    instr: ResetInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    reset_expr = _coil_reset_value(instr.target, ctx)
    writes = _compile_target_write_lines(instr.target, reset_expr, ctx, 0)
    return [f"{' ' * indent}{line}" for line in emit_if_enabled(enabled_expr, writes, fold=False)]


def _compile_out_instruction(
    instr: OutInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    # Each range write takes the next local name, so build the writes in output order.
    # A literal True enable never runs the off side, so do not build it.
    always = _bool_literal(enabled_expr) is True
    if instr.oneshot:
        key = f"_oneshot:{ctx.state_key_for(instr)}"
        if ctx._current_function is not None:
            ctx.mark_function_global(ctx._current_function, "_mem")
        off = [] if always else _compile_target_write_lines(instr.target, "False", ctx, 0)
        fire = _compile_target_write_lines(instr.target, "True", ctx, 0)
        held = _compile_target_write_lines(instr.target, "False", ctx, 0)
        oneshot = emit_oneshot(key, enabled_expr, fire=fire, off=off, held=held)
        return [f"{sp}{line}" for line in oneshot]

    on = _compile_target_write_lines(instr.target, "True", ctx, 0)
    off = [] if always else _compile_target_write_lines(instr.target, "False", ctx, 0)
    return [f"{sp}{line}" for line in emit_if_enabled(enabled_expr, on, otherwise=off)]


def _compile_call_instruction(
    instr: CallInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    sp = " " * indent
    fn = _subroutine_symbol(instr.subroutine_name)
    if ctx.blockless:
        call_expr = f"{fn}(tags, _mem, _prev, dt)"
    elif ctx.kernel_runtime:
        call_expr = f"{fn}(tags)"
    else:
        call_expr = f"{fn}()"
    return [f"{sp}{line}" for line in emit_if_enabled(enabled_expr, [call_expr])]


def _compile_return_instruction(enabled_expr: str, indent: int) -> list[str]:
    sp = " " * indent
    return [f"{sp}{line}" for line in emit_if_enabled(enabled_expr, ["return"])]


def _reset_expr(
    instr: OnDelayInstruction | CountUpInstruction | CountDownInstruction, ctx: CodegenContext
) -> str | None:
    if instr.reset_condition is None:
        return None
    return _get_condition_snapshot(instr, "reset_condition", ctx) or compile_condition(
        instr.reset_condition, ctx
    )


def _rest_or_run(reset_expr: str | None, rest: list[str], run: list[str]) -> list[str]:
    """Return lines that run *rest* on a scan where the reset is on, and *run* on any other."""
    if reset_expr is None:
        return run
    return emit_if_enabled(reset_expr, rest, otherwise=run, fold=False)


def _compile_on_delay_instruction(
    instr: OnDelayInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done_write = _compile_lvalue(instr.done_bit, ctx)
    acc_read = _compile_value(instr.accumulator, ctx)
    acc_write = _compile_lvalue(instr.accumulator, ctx)
    acc = instr.accumulator
    preset = _compile_value(instr.preset, ctx)
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    sp = " " * indent
    reset_expr = _reset_expr(instr, ctx)
    rest = emit_acc_rest(done_write, acc_write, owner=instr, ctx=ctx, frac=True)
    run = [
        *emit_tick(acc, instr.unit, ctx, acc_read=acc_read),
        *emit_done(
            done_write,
            acc_write,
            ">=",
            preset,
            owner=instr,
            ctx=ctx,
            preset_key=_preset_key(instr, ctx),
            frac=True,
        ),
    ]
    otherwise = rest if reset_expr is None else []
    run = emit_if_enabled(enabled_expr, run, otherwise=otherwise, fold=False)
    lines = [f"{sp}{line}" for line in _rest_or_run(reset_expr, rest, run)]
    # Status bits: computed after all branches using final tag values
    done_read = _compile_value(instr.done_bit, ctx)
    if reset_expr is not None:
        tt_expr = f"bool({enabled_expr}) and not bool({reset_expr}) and not {done_read}"
    else:
        tt_expr = f"bool({enabled_expr}) and not {done_read}"
    lines.extend(
        _compile_status_bit_lines(
            instr, ctx, indent, en_bit=f"bool({enabled_expr})", tt_bit=tt_expr
        )
    )
    return lines


def _compile_off_delay_instruction(
    instr: OffDelayInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done_read = _compile_value(instr.done_bit, ctx)
    done_write = _compile_lvalue(instr.done_bit, ctx)
    acc_read = _compile_value(instr.accumulator, ctx)
    acc_write = _compile_lvalue(instr.accumulator, ctx)
    acc = instr.accumulator
    armed_key = tof_armed_key(instr.accumulator.name)
    preset = _compile_value(instr.preset, ctx)
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    sp = " " * indent
    sp4 = " " * (indent + 4)
    sp8 = " " * (indent + 8)
    sp12 = " " * (indent + 12)
    tick = emit_tick(acc, instr.unit, ctx, acc_read=None)
    done = emit_done(
        done_write,
        acc_write,
        "<",
        preset,
        owner=instr,
        ctx=ctx,
        preset_key=_preset_key(instr, ctx),
        frac=True,
        paren=False,
    )
    on = emit_acc_rest(done_write, acc_write, owner=instr, ctx=ctx, frac=True, done_value=True)
    moved = emit_cmp("_acc", "!=", "0", paren=False, ctx=ctx, terms=(TagValue(acc.name), Const(0)))
    # A TOF never enabled rests at Done False, Acc 0 (see tof_armed_key).
    lines = [
        f"{sp}_tof_armed = {emit_mem_get(armed_key, 'False', ctx=ctx)}",
        f"{sp}if {enabled_expr}:",
        f"{sp4}if not _tof_armed:",
        f"{sp8}{emit_mem_set(armed_key, 'True', quote='double', ctx=ctx)}",
        *(f"{sp4}{line}" for line in on),
        f"{sp}else:",
        f"{sp4}_acc = int({acc_read})",
        f"{sp4}if _tof_armed or {done_read} or {moved}:",
        f"{sp8}if not _tof_armed:",
        f"{sp12}{emit_mem_set(armed_key, 'True', quote='double', ctx=ctx)}",
        *(f"{sp8}{line}" for line in (*tick, *done)),
    ]
    # TOF status bits: EN = enabled, TT = not enabled and done (timing while off)
    tt_expr = f"not bool({enabled_expr}) and {emit_truth(done_read)}"
    lines.extend(
        _compile_status_bit_lines(
            instr, ctx, indent, en_bit=f"bool({enabled_expr})", tt_bit=tt_expr
        )
    )
    return lines


def _preset_key(
    instr: OnDelayInstruction | OffDelayInstruction | CountUpInstruction | CountDownInstruction,
    ctx: CodegenContext,
) -> str | None:
    """Return the memory key that holds a timer's or counter's preset for the prover.

    Return None unless the kernel is built with ``proof_metadata``.
    """
    return prove_effective_preset_key(instr.done_bit.name) if ctx.proof_metadata else None


def _count_edge_lines(
    name: str, key: str, input_expr: str, indent: int, ctx: CodegenContext
) -> list[str]:
    """Bind *name* to a counter input's rising edge and update its memory.

    The memory is the input as the counter last scanned it.  It is written on every scan
    that the counter runs, reset scans included.  It starts off, so an input that is
    already on at the first scan counts (see ``counters.count_edge``).
    """
    sp = " " * indent
    now = f"{name}_now"
    return [
        f"{sp}{now} = bool({input_expr})",
        f"{sp}{name} = {emit_rise(now, key, cast=False, ctx=ctx)}",
        f"{sp}{emit_mem_set(key, now, ctx=ctx)}",
    ]


def _compile_count_up_instruction(
    instr: CountUpInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done_write = _compile_lvalue(instr.done_bit, ctx)
    acc_read = _compile_value(instr.accumulator, ctx)
    acc_write = _compile_lvalue(instr.accumulator, ctx)
    preset = _compile_value(instr.preset, ctx)
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    site = ctx.state_key_for(instr)
    up_key = f"{COUNT_UP_MEMORY}:{site}"
    lines: list[str] = _count_edge_lines("_cu_edge", up_key, enabled_expr, indent, ctx)
    down_expr: str | None = None
    if instr.down_condition is not None:
        down_expr = _get_condition_snapshot(instr, "down_condition", ctx) or compile_condition(
            instr.down_condition, ctx
        )
        lines.extend(
            _count_edge_lines("_cd_edge", f"{COUNT_DOWN_MEMORY}:{site}", down_expr, indent, ctx)
        )
    rest = emit_acc_rest(done_write, acc_write, owner=instr, ctx=ctx)
    down_edge = None if down_expr is None else "_cd_edge"
    run = [
        *emit_count(instr.accumulator, acc_read, up="_cu_edge", down=down_edge, ctx=ctx),
        *emit_done(
            done_write,
            acc_write,
            ">=",
            preset,
            owner=instr,
            ctx=ctx,
            preset_key=_preset_key(instr, ctx),
        ),
    ]
    sp = " " * indent
    lines.extend(f"{sp}{line}" for line in _rest_or_run(_reset_expr(instr, ctx), rest, run))
    # Counter status bits: CU = enabled, CD = down condition active
    cd_expr = f"bool({down_expr})" if instr.down_condition is not None else "False"
    lines.extend(
        _compile_status_bit_lines(
            instr, ctx, indent, cu_bit=f"bool({enabled_expr})", cd_bit=cd_expr
        )
    )
    return lines


def _compile_count_down_instruction(
    instr: CountDownInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    done_write = _compile_lvalue(instr.done_bit, ctx)
    acc_read = _compile_value(instr.accumulator, ctx)
    acc_write = _compile_lvalue(instr.accumulator, ctx)
    preset = _compile_value(instr.preset, ctx)
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")
    down_key = f"{COUNT_DOWN_MEMORY}:{ctx.state_key_for(instr)}"
    lines: list[str] = _count_edge_lines("_cd_edge", down_key, enabled_expr, indent, ctx)
    rest = emit_acc_rest(done_write, acc_write, owner=instr, ctx=ctx)
    run = [
        *emit_count(instr.accumulator, acc_read, up=None, down="_cd_edge", ctx=ctx),
        *emit_done(
            done_write,
            acc_write,
            "<=",
            preset,
            owner=instr,
            ctx=ctx,
            preset_key=_preset_key(instr, ctx),
        ),
    ]
    sp = " " * indent
    lines.extend(f"{sp}{line}" for line in _rest_or_run(_reset_expr(instr, ctx), rest, run))
    # Counter status bits: CU = False (always), CD = enabled
    lines.extend(
        _compile_status_bit_lines(
            instr, ctx, indent, cu_bit="False", cd_bit=f"bool({enabled_expr})"
        )
    )
    return lines


def _copy_target(dest: Dest) -> Tag | IndirectRef | IndirectExprRef:
    """Return the tag or the pointer that a Copy writes, as the shared helpers take it.

    The generated code writes an ``immediate()`` tag as it writes the plain tag.
    """
    match dest:
        case TagArg(tag=tag):
            return tag
        case PointerArg(ref=ref):
            return ref
        case _:
            assert_never(dest)


def _copy_source(source: Value) -> Any:
    """Return what a Copy reads, as ``_compile_value`` and the pointer helpers take it."""
    match source:
        case TagArg(tag=tag):
            return tag
        case PointerArg(ref=ref):
            return ref
        case ExprArg(expr=expr):
            return expr
        case ConstArg(value=value):
            return value
        case _:
            assert_never(source)


def _compile_copy_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    source = instr.source
    target = _copy_target(instr.dest)
    target_type = _value_type_name(target)
    # A text literal into a CHAR fans out, one character per register.
    fanout = (
        isinstance(source, ConstArg)
        and isinstance(source.value, str)
        and len(source.value) > 1
        and target_type == "CHAR"
    )
    convert = instr.convert
    if (
        convert is not None
        and convert.mode == "binary"
        and copy_binary_store(source, TagType[target_type]) == BINARY_VALUE
    ):
        # A Copy Binary of a constant into a number stores what a plain Copy stores.
        convert = None
    if convert is not None or fanout:
        return _compile_copy_converter_instruction(instr, enabled_expr, ctx, indent, convert)
    source_expr = _compile_copy_source(_copy_source(source), ctx)
    if ctx.kernel_runtime and target_type == "REAL" and isinstance(source, ExprArg):
        ctx.mark_helper("_computed_real")
        source_expr = f"_computed_real({source_expr})"
    match source:
        case ConstArg(value=str()):
            source_type: str | None = "CHAR"
        case TagArg() | PointerArg() | ConstArg() | ExprArg():
            source_type = _optional_value_type_name(source)
        case _:
            assert_never(source)
    source_kind = copy_source_kind(source)
    clamp_cond = _copy_clamp_condition("_copy_value", target_type, source_type)
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    if clamp_cond is None or fault_body == ["pass"] or _copy_source_fits(source, target_type):
        value_expr = _copy_store_expr(source_expr, target_type, source_kind, ctx)
        enabled_body = _compile_assignment_lines(target, value_expr, ctx, indent=0)
    else:
        value_expr = _copy_store_expr("_copy_value", target_type, source_kind, ctx)
        enabled_body = [
            f"_copy_value = {source_expr}",
            *_compile_assignment_lines(target, value_expr, ctx, indent=0),
            f"if {clamp_cond}:",
            *_indent_body(fault_body, 4),
        ]
    if isinstance(source, PointerArg) and _past_end_keeps(source.ref):
        # A source pointer past its bank's end drops the copy with no flag, but stepping
        # flags a target pointer below its bank first.
        target_check = _pointer_check_lines(target, ctx, past_end_drops=False)
        enabled_body = [*target_check, *enabled_body]
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)


def _copy_source_fits(source: Value, target_type: str) -> bool:
    """Return True for a literal that the store leaves alone, so it needs no clamp test."""
    if not isinstance(source, ConstArg) or type(source.value) is not int:
        return False
    value = source.value
    if target_type == "INT":
        return -32768 <= value <= 32767
    return -2147483648 <= value <= 2147483647


def _compile_copy_converter_instruction(
    instr: CopyInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
    converter: CopyConverter | None = None,
) -> list[str]:
    """Compile a Copy with *converter* (``instr.convert``, or the plain one across text).

    When *converter* is ``None``, compile a text literal's fan-out instead.
    """
    if converter is not None and not isinstance(converter, CopyConverter):
        raise TypeError("copy converter compiler requires CopyConverter")

    stem = ctx.next_name("copymod")
    address_fault_body = _compile_set_address_error_fault_body(ctx)
    fault_body = _compile_set_out_of_range_fault_body(ctx)
    mode = "fanout" if converter is None else converter.mode
    source = _copy_source(instr.source)
    source_expr = _compile_copy_source(source, ctx)
    source_read: list[str] = []
    if _past_end_keeps(source):
        # Read the source where a pointer past the end drops the copy (the guard's IndexError).
        # Reading it inside the conversion's own handler would flag it.
        source_read = [f"_{stem}_source = {source_expr}"]
        source_expr = f"_{stem}_source"

    target = _copy_target(instr.dest)
    setup, target_kind, target_symbol, target_start_var, target_type = _copy_converter_target_info(
        target, ctx, stem
    )
    moves_bits = (
        mode == "binary" and copy_binary_store(instr.source, TagType[target_type]) == BINARY_BITS
    )
    if mode in {"text", "binary"} and target_type != "CHAR" and not moves_bits:
        # Only a TXT holds text, so stepping sets the out-of-range flag and writes nothing.
        # It reads no source, but a source pointer past its bank's end drops the copy.
        past_end = _copy_source_past_end_test(source, ctx)
        body = [*setup, *fault_body]
        if past_end is not None:
            body = [*setup, f"if not ({past_end}):", *_indent_body(fault_body, 4)]
        return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, body)
    values_var = f"_{stem}_values"
    target_name = target.name if isinstance(target, Tag) else None
    fit = block_room(target) if isinstance(target, Tag) else None
    following_names = (
        element_names_after(target, fit)
        if isinstance(target, Tag) and fit is not None and target_kind == "scalar"
        else None
    )
    sequential_reloads = (
        _compute_sequential_reloads(target_name, ctx, following_names)
        if target_name and target_kind == "scalar"
        else None
    )
    write_lines = _copy_converter_write_lines(
        values_var=values_var,
        target_kind=target_kind,
        target_symbol=target_symbol,
        target_start_var=target_start_var,
        fault_body=fault_body,
        target_name=target_name,
        sequential_reloads=sequential_reloads,
        kernel=ctx.kernel_runtime,
        fit=fit,
        following_names=following_names,
    )

    # Stepping resolves the target first, and a target pointer past its bank drops the
    # copy before the source is read.
    enabled_body: list[str] = [*setup, *source_read]
    if mode == "fanout":
        enabled_body.extend(
            [
                "try:",
                f"    {values_var} = [{_store_coerce_expr('_ch', target_type, ctx)} for _ch in {source_expr}]",
                *_indent_body(write_lines, 4),
                "except IndexError:",
                *_indent_body(address_fault_body, 4),
                "except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif converter is None:
        raise RuntimeError(f"unhandled copy mode {mode!r}")
    elif mode in {"value", "ascii"}:
        ctx.mark_helper("_text_from_source_value")
        ctx.mark_helper("_store_numeric_text_digit")
        enabled_body.extend(
            [
                "try:",
                f"    _copy_text = _text_from_source_value({source_expr})",
                f"    {values_var} = []",
                "    for _copy_char in _copy_text:",
                f'        _copy_numeric = _store_numeric_text_digit(_copy_char, "{mode}")',
                f"        {values_var}.append({_store_coerce_expr('_copy_numeric', target_type, ctx)})",
                *_indent_body(write_lines, 4),
                "except IndexError:",
                *_indent_body(address_fault_body, 4),
                "except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "text":
        text_lines = _number_text_lines("_rendered", source_expr, instr.source, converter, ctx)
        enabled_body.extend(
            [
                "try:",
                *_indent_body(text_lines, 4),
                f"    {values_var} = [{_store_coerce_expr('_ch', target_type, ctx)} for _ch in _rendered]",
                *_indent_body(write_lines, 4),
                "except IndexError:",
                *_indent_body(address_fault_body, 4),
                "except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif moves_bits:
        if target_type == "DINT":
            ctx.mark_helper("_float_to_int_bits")
            source_expr = f"_float_to_int_bits({source_expr})"
        stored = _pack_store_expr(source_expr, target_type, ctx)
        enabled_body.extend(
            [
                "try:",
                f"    {values_var} = [{stored}]",
                *_indent_body(write_lines, 4),
                "except IndexError:",
                *_indent_body(address_fault_body, 4),
                "except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    elif mode == "binary":
        ctx.mark_helper("_ascii_char_from_code")
        enabled_body.extend(
            [
                "try:",
                f"    _copy_char = _ascii_char_from_code(int({source_expr}) & 0xFF)",
                f"    {values_var} = [{_store_coerce_expr('_copy_char', target_type, ctx)}]",
                *_indent_body(write_lines, 4),
                "except IndexError:",
                *_indent_body(address_fault_body, 4),
                "except (TypeError, ValueError, OverflowError):",
                *_indent_body(fault_body, 4),
            ]
        )
    else:
        enabled_body.extend(fault_body)
    return _compile_guarded_instruction(instr, enabled_expr, ctx, indent, enabled_body)
