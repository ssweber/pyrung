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
from pyrung.core.instruction.out_variants import _FLASH_IDLE, _FLASH_OFF, _FLASH_ON
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

from ._primitives import (
    _compile_lvalue,
    _compile_target_write_lines,
    _compile_value,
    _timer_carry_lines,
)


def _use_memory(ctx: CodegenContext) -> None:
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")


def _tick_lines(
    acc: Tag, preset: Tag | int, unit: TimeUnit, ctx: CodegenContext, sp: str
) -> list[str]:
    """Advance *acc* by this scan's dt into ``_acc``; leave the remainder in ``_new_frac``.

    The prover's abstract kernel lets its oracle choose the advance toward
    *preset* instead (``render_kernel.compile_kernel``), with no remainder.
    """
    if ctx.abstract_timers:
        return [
            f"{sp}_new_frac = 0",
            f"{sp}_acc = _bb_tick(_mem, {acc.name!r}, int({_compile_value(acc, ctx)}),"
            f" int({_compile_value(preset, ctx)}))",
        ]
    return [
        *_timer_carry_lines(acc, unit, ctx, sp),
        f"{sp}_acc = min(int({_compile_value(acc, ctx)}) + _int_units, {_INT_MAX})",
    ]


def _keep_frac_line(acc: Tag, sp: str) -> str:
    frac_key = f"_frac:{acc.name}"
    return f'{sp}_mem["{frac_key}"] = _mem.get("{frac_key}", 0)'


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
        f"{sp}if _en and not bool(_mem.get({key!r}, False)):",
    ]
    lines.extend(
        _compile_target_write_lines(
            instr.target, f"not bool({_compile_value(target, ctx)})", ctx, indent + 4
        )
    )
    lines.append(f"{sp}_mem[{key!r}] = _en")
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
    rung_key = f"_debounce_rung:{ctx.state_key_for(instr)}"
    on_frac = f"_frac:{instr.on_acc.name}"
    off_frac = f"_frac:{instr.off_acc.name}"
    on_write = _compile_lvalue(instr.on_acc, ctx)
    off_write = _compile_lvalue(instr.off_acc, ctx)
    target_write = _compile_lvalue(instr.target, ctx)
    return [
        f"{sp}_en = bool({enabled_expr})",
        f"{sp}_was = bool(_mem.get({rung_key!r}, False))",
        f"{sp}_mem[{rung_key!r}] = _en",
        f"{sp}if _en:",
        f"{sp4}if not _was:",
        # A rise starts the on side from 0.
        f"{sp8}{on_write} = 0",
        f'{sp8}_mem["{on_frac}"] = 0',
        *_tick_lines(instr.on_acc, instr.on_preset, instr.unit, ctx, sp4),
        f'{sp4}_mem["{on_frac}"] = _new_frac',
        f'{sp4}_mem["{off_frac}"] = 0',
        f"{sp4}{on_write} = _acc",
        f"{sp4}{off_write} = 0",
        f"{sp4}if _acc >= int({_compile_value(instr.on_preset, ctx)}):",
        f"{sp8}{target_write} = True",
        f"{sp}else:",
        # The off side compares what it had reached before this scan, and not on the fall.
        f"{sp4}_timed_out = not _was and int({_compile_value(instr.off_acc, ctx)})"
        f" >= int({_compile_value(instr.off_preset, ctx)})",
        *_tick_lines(instr.off_acc, instr.off_preset, instr.unit, ctx, sp4),
        f'{sp4}_mem["{off_frac}"] = _new_frac',
        _keep_frac_line(instr.on_acc, sp4),
        f"{sp4}{off_write} = _acc",
        f"{sp4}if _timed_out:",
        f"{sp8}{target_write} = False",
    ]


def _compile_flasher_phase(
    instr: FlasherInstruction,
    on_phase: bool,
    ctx: CodegenContext,
    sp: str,
) -> list[str]:
    """The running phase's tick; at or past its preset, the other phase starts from 0."""
    sp4 = sp + "    "
    on_frac = f"_frac:{instr.on_acc.name}"
    off_frac = f"_frac:{instr.off_acc.name}"
    if on_phase:
        acc, other, preset, limit, other_phase = (
            instr.on_acc,
            instr.off_acc,
            instr.on_preset,
            "_on_preset",
            _FLASH_OFF,
        )
    else:
        acc, other, preset, limit, other_phase = (
            instr.off_acc,
            instr.on_acc,
            instr.off_preset,
            "_off_preset",
            _FLASH_ON,
        )
    return [
        *_tick_lines(acc, preset, instr.unit, ctx, sp),
        f"{sp}{_compile_lvalue(acc, ctx)} = _acc",
        f"{sp}if _acc >= {limit}:",
        f"{sp4}_phase = {other_phase}",
        f"{sp4}{_compile_lvalue(other, ctx)} = 0",
        f'{sp4}_mem["{on_frac}"] = 0',
        f'{sp4}_mem["{off_frac}"] = 0',
        f"{sp}else:",
        f'{sp4}_mem["_frac:{acc.name}"] = _new_frac',
        _keep_frac_line(other, sp4),
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
    on_frac = f"_frac:{instr.on_acc.name}"
    off_frac = f"_frac:{instr.off_acc.name}"
    phase_key = f"_flash_phase:{ctx.state_key_for(instr)}"
    on_write = _compile_lvalue(instr.on_acc, ctx)
    off_write = _compile_lvalue(instr.off_acc, ctx)
    target_write = _compile_lvalue(instr.target, ctx)
    return [
        f"{sp}if not bool({enabled_expr}):",
        f'{sp4}_mem["{phase_key}"] = {_FLASH_IDLE}',
        f"{sp4}{target_write} = False",
        f"{sp}else:",
        f"{sp4}_on_preset = int({_compile_value(instr.on_preset, ctx)})",
        f"{sp4}_off_preset = int({_compile_value(instr.off_preset, ctx)})",
        f"{sp4}if _on_preset <= 0 or _off_preset <= 0:",
        # Held reset: idle, dark, both accumulators 0.
        f'{sp8}_mem["{phase_key}"] = {_FLASH_IDLE}',
        f'{sp8}_mem["{on_frac}"] = 0',
        f'{sp8}_mem["{off_frac}"] = 0',
        f"{sp8}{on_write} = 0",
        f"{sp8}{off_write} = 0",
        f"{sp8}{target_write} = False",
        f"{sp4}else:",
        f'{sp8}_phase = _mem.get("{phase_key}", {_FLASH_IDLE})',
        f"{sp8}if _phase == {_FLASH_IDLE}:",
        f"{sp12}_phase = {_FLASH_ON}",
        f"{sp12}{on_write} = 0",
        f"{sp12}{off_write} = 0",
        f'{sp12}_mem["{on_frac}"] = 0',
        f'{sp12}_mem["{off_frac}"] = 0',
        f"{sp8}if _phase == {_FLASH_ON}:",
        *_compile_flasher_phase(instr, True, ctx, sp12),
        f"{sp8}else:",
        *_compile_flasher_phase(instr, False, ctx, sp12),
        f'{sp8}_mem["{phase_key}"] = _phase',
        f"{sp8}{target_write} = _phase == {_FLASH_ON}",
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
    frac_key = f"_frac:{instr.timer_acc.name}"
    rung_key = f"_timed_rung:{ctx.state_key_for(instr)}"
    target_write = _compile_lvalue(instr.target, ctx)
    acc_write = _compile_lvalue(instr.timer_acc, ctx)
    timing_target = "(_acc < _t_preset or _en)" if instr.maintained else "(_acc < _t_preset)"
    held_target = "_en" if instr.maintained else "False"
    return [
        f"{sp}_en = bool({enabled_expr})",
        f"{sp}_rose = _en and not bool(_mem.get({rung_key!r}, False))",
        f"{sp}_mem[{rung_key!r}] = _en",
        f"{sp}_t_preset = int({_compile_value(instr.on_preset, ctx)})",
        f"{sp}_timing = bool({_compile_value(instr.target, ctx)}) and (",
        f"{sp4}int({_compile_value(instr.timer_acc, ctx)}) < _t_preset",
        f"{sp})",
        f"{sp}if _rose and not _timing:",
        f"{sp4}_timing = True",
        f"{sp4}{acc_write} = 0",
        f'{sp4}_mem["{frac_key}"] = 0',
        f"{sp}if _timing:",
        *_tick_lines(instr.timer_acc, instr.on_preset, instr.unit, ctx, sp4),
        f'{sp4}_mem["{frac_key}"] = _new_frac',
        f"{sp4}{target_write} = {timing_target}",
        f"{sp4}{acc_write} = _acc",
        f"{sp}else:",
        _keep_frac_line(instr.timer_acc, sp4),
        f"{sp4}{target_write} = {held_target}",
    ]
