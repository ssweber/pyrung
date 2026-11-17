"""Compiled Out coil variants: toggle, debounce, flasher and timed.

Each function mirrors its instruction's ``execute`` line for line, including
which memory keys it writes on every scan, so the interpreted and compiled
backends agree on tags and memory.
"""

from __future__ import annotations

from pyrung.circuitpy.codegen._constants import _INT_MAX
from pyrung.circuitpy.codegen.context import CodegenContext
from pyrung.core.instruction import (
    DebounceInstruction,
    FlasherInstruction,
    TimedInstruction,
    ToggleInstruction,
)
from pyrung.core.instruction.out_variants import (
    _FLASH_IDLE,
    _FLASH_OFF,
    _FLASH_OFF_NEXT,
    _FLASH_ON,
    _FLASH_ON_NEXT,
)
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

from ._emit import emit_frac, emit_frac_keep, emit_mem_get, emit_mem_set
from ._primitives import (
    _compile_lvalue,
    _compile_target_write_lines,
    _compile_value,
    _timer_carry_lines,
)


def _use_memory(ctx: CodegenContext) -> None:
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")


def _tick_lines(acc: Tag, unit: TimeUnit, ctx: CodegenContext, sp: str) -> list[str]:
    """Advance *acc* by this scan's dt into ``_acc``; leave the remainder in ``_new_frac``."""
    return [
        *_timer_carry_lines(acc, unit, ctx, sp),
        f"{sp}_acc = min(int({_compile_value(acc, ctx)}) + _int_units, {_INT_MAX})",
    ]


def _compile_toggle_instruction(
    instr: ToggleInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    _use_memory(ctx)
    sp = " " * indent
    key = f"_toggle:{ctx.state_key_for(instr)}"
    target = instr.target.value if not isinstance(instr.target, Tag) else instr.target
    assert isinstance(target, Tag)
    lines = [
        f"{sp}_en = bool({enabled_expr})",
        f"{sp}if _en and not bool({emit_mem_get(key, 'False')}):",
    ]
    lines.extend(
        _compile_target_write_lines(
            instr.target, f"not bool({_compile_value(target, ctx)})", ctx, indent + 4
        )
    )
    lines.append(f"{sp}{emit_mem_set(key, '_en')}")
    return lines


def _compile_debounce_instruction(
    instr: DebounceInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    _use_memory(ctx)
    sp = " " * indent
    sp4 = " " * (indent + 4)
    sp8 = " " * (indent + 8)
    sp12 = " " * (indent + 12)
    rung_key = f"_debounce_rung:{ctx.state_key_for(instr)}"
    on_write = _compile_lvalue(instr.on_acc, ctx)
    off_write = _compile_lvalue(instr.off_acc, ctx)
    target_write = _compile_lvalue(instr.target, ctx)
    return [
        f"{sp}_en = bool({enabled_expr})",
        f"{sp}_was = bool({emit_mem_get(rung_key, 'False')})",
        f"{sp}{emit_mem_set(rung_key, '_en')}",
        f"{sp}if _en:",
        f"{sp4}if not _was:",
        # A rise starts the on side from 0.
        f"{sp8}{on_write} = 0",
        f"{sp8}{emit_frac(instr.on_acc, '0', quote='double')}",
        *_tick_lines(instr.on_acc, instr.unit, ctx, sp4),
        f"{sp4}{emit_frac(instr.on_acc, '_new_frac', quote='double')}",
        f"{sp4}{emit_frac(instr.off_acc, '0', quote='double')}",
        f"{sp4}{on_write} = _acc",
        f"{sp4}{off_write} = 0",
        f"{sp4}if _acc >= int({_compile_value(instr.on_preset, ctx)}):",
        f"{sp8}{target_write} = True",
        f"{sp}else:",
        f"{sp4}{emit_frac_keep(instr.on_acc)}",
        # With the coil off, the off TD holds its value, including one the ladder wrote.
        f"{sp4}if not bool({_compile_value(instr.target, ctx)}):",
        f"{sp8}{emit_frac_keep(instr.off_acc)}",
        f"{sp4}else:",
        # The off side compares what it had reached before this scan, and not on the fall.
        f"{sp8}_timed_out = not _was and int({_compile_value(instr.off_acc, ctx)})"
        f" >= int({_compile_value(instr.off_preset, ctx)})",
        *_tick_lines(instr.off_acc, instr.unit, ctx, sp8),
        f"{sp8}{emit_frac(instr.off_acc, '_new_frac', quote='double')}",
        f"{sp8}{off_write} = _acc",
        f"{sp8}if _timed_out:",
        f"{sp12}{target_write} = False",
    ]


def _compile_flasher_phase(
    instr: FlasherInstruction,
    on_phase: bool,
    ctx: CodegenContext,
    sp: str,
) -> list[str]:
    """The running phase's tick.  At or past its preset, the other phase runs from the next scan."""
    sp4 = sp + "    "
    if on_phase:
        acc, other, limit, other_phase = (
            instr.on_acc,
            instr.off_acc,
            "_on_preset",
            _FLASH_OFF_NEXT,
        )
    else:
        acc, other, limit, other_phase = (
            instr.off_acc,
            instr.on_acc,
            "_off_preset",
            _FLASH_ON_NEXT,
        )
    return [
        *_tick_lines(acc, instr.unit, ctx, sp),
        f"{sp}{_compile_lvalue(acc, ctx)} = _acc",
        f"{sp}if _acc >= {limit}:",
        f"{sp4}_phase = {other_phase}",
        f"{sp4}{emit_frac(instr.on_acc, '0', quote='double')}",
        f"{sp4}{emit_frac(instr.off_acc, '0', quote='double')}",
        f"{sp}else:",
        f"{sp4}{emit_frac(acc, '_new_frac', quote='double')}",
        f"{sp4}{emit_frac_keep(other)}",
    ]


def _compile_flasher_instruction(
    instr: FlasherInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    _use_memory(ctx)
    sp = " " * indent
    sp4 = " " * (indent + 4)
    sp8 = " " * (indent + 8)
    sp12 = " " * (indent + 12)
    phase_key = f"_flash_phase:{ctx.state_key_for(instr)}"
    on_write = _compile_lvalue(instr.on_acc, ctx)
    off_write = _compile_lvalue(instr.off_acc, ctx)
    target_write = _compile_lvalue(instr.target, ctx)
    return [
        f"{sp}if not bool({enabled_expr}):",
        f"{sp4}{emit_mem_set(phase_key, str(_FLASH_IDLE), quote='double')}",
        f"{sp4}{target_write} = False",
        f"{sp}else:",
        f"{sp4}_on_preset = int({_compile_value(instr.on_preset, ctx)})",
        f"{sp4}_off_preset = int({_compile_value(instr.off_preset, ctx)})",
        f"{sp4}if _on_preset <= 0 or _off_preset <= 0:",
        # A preset of 0 or below holds the flasher idle and dark and freezes both accumulators.
        f"{sp8}{emit_mem_set(phase_key, str(_FLASH_IDLE), quote='double')}",
        f"{sp8}{target_write} = False",
        f"{sp4}else:",
        f"{sp8}_phase = {emit_mem_get(phase_key, str(_FLASH_IDLE), quote='double')}",
        f"{sp8}if _phase == {_FLASH_IDLE}:",
        f"{sp12}_phase = {_FLASH_ON}",
        f"{sp12}{on_write} = 0",
        f"{sp12}{off_write} = 0",
        f"{sp12}{emit_frac(instr.on_acc, '0', quote='double')}",
        f"{sp12}{emit_frac(instr.off_acc, '0', quote='double')}",
        # The phase that starts now runs from 0, and the other's accumulator holds.
        f"{sp8}elif _phase == {_FLASH_ON_NEXT}:",
        f"{sp12}_phase = {_FLASH_ON}",
        f"{sp12}{on_write} = 0",
        f"{sp12}{emit_frac(instr.on_acc, '0', quote='double')}",
        f"{sp8}elif _phase == {_FLASH_OFF_NEXT}:",
        f"{sp12}_phase = {_FLASH_OFF}",
        f"{sp12}{off_write} = 0",
        f"{sp12}{emit_frac(instr.off_acc, '0', quote='double')}",
        # The coil shows the phase that runs this scan, even one that ends on it.
        f"{sp8}{target_write} = _phase == {_FLASH_ON}",
        f"{sp8}if _phase == {_FLASH_ON}:",
        *_compile_flasher_phase(instr, True, ctx, sp12),
        f"{sp8}else:",
        *_compile_flasher_phase(instr, False, ctx, sp12),
        f"{sp8}{emit_mem_set(phase_key, '_phase', quote='double')}",
    ]


def _compile_timed_instruction(
    instr: TimedInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    _use_memory(ctx)
    sp = " " * indent
    sp4 = " " * (indent + 4)
    rung_key = f"_timed_rung:{ctx.state_key_for(instr)}"
    target_write = _compile_lvalue(instr.target, ctx)
    acc_write = _compile_lvalue(instr.timer_acc, ctx)
    timing_target = "(_acc < _t_preset or _en)" if instr.maintained else "(_acc < _t_preset)"
    held_target = "_en" if instr.maintained else "False"
    return [
        f"{sp}_en = bool({enabled_expr})",
        f"{sp}_rose = _en and not bool({emit_mem_get(rung_key, 'False')})",
        f"{sp}{emit_mem_set(rung_key, '_en')}",
        f"{sp}_t_preset = int({_compile_value(instr.on_preset, ctx)})",
        f"{sp}_timing = bool({_compile_value(instr.target, ctx)}) and (",
        f"{sp4}int({_compile_value(instr.timer_acc, ctx)}) < _t_preset",
        f"{sp})",
        f"{sp}if _rose and not _timing:",
        f"{sp4}_timing = True",
        f"{sp4}{acc_write} = 0",
        f"{sp4}{emit_frac(instr.timer_acc, '0', quote='double')}",
        f"{sp}if _timing:",
        *_tick_lines(instr.timer_acc, instr.unit, ctx, sp4),
        f"{sp4}{emit_frac(instr.timer_acc, '_new_frac', quote='double')}",
        f"{sp4}{target_write} = {timing_target}",
        f"{sp4}{acc_write} = _acc",
        f"{sp}else:",
        f"{sp4}{emit_frac_keep(instr.timer_acc)}",
        f"{sp4}{target_write} = {held_target}",
    ]
