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
from pyrung.core.tag import Tag
from pyrung.core.time_mode import TimeUnit

from ._primitives import (
    _compile_lvalue,
    _compile_target_write_lines,
    _compile_value,
    _timer_dt_to_units_expr,
)


def _use_memory(ctx: CodegenContext) -> None:
    if ctx._current_function is not None:
        ctx.mark_function_global(ctx._current_function, "_mem")


def _tick_lines(acc: Tag, unit: TimeUnit, ctx: CodegenContext, sp: str) -> list[str]:
    """Advance *acc* by this scan's dt into ``_acc``; leave the remainder in ``_rem``."""
    frac_key = f"_frac:{acc.name}"
    return [
        f'{sp}_frac = float(_mem.get("{frac_key}", 0.0))',
        f'{sp}_dt = float(_mem.get("_dt", 0.0))',
        f"{sp}_dt_units = {_timer_dt_to_units_expr(unit, '_dt', '_frac')}",
        f"{sp}_int_units = int(_dt_units)",
        f"{sp}_rem = _dt_units - _int_units",
        f"{sp}_acc = min(int({_compile_value(acc, ctx)}) + _int_units, {_INT_MAX})",
    ]


def _keep_frac_line(acc: Tag, sp: str) -> str:
    frac_key = f"_frac:{acc.name}"
    return f'{sp}_mem["{frac_key}"] = float(_mem.get("{frac_key}", 0.0))'


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


def _compile_debounce_side(
    timing: str,
    done: Tag,
    acc: Tag,
    preset: Tag | int,
    ready: str,
    unit: TimeUnit,
    ctx: CodegenContext,
    sp: str,
) -> list[str]:
    sp4 = sp + "    "
    frac_key = f"_frac:{acc.name}"
    done_write = _compile_lvalue(done, ctx)
    acc_write = _compile_lvalue(acc, ctx)
    return [
        f"{sp}if {timing}:",
        *_tick_lines(acc, unit, ctx, sp4),
        f"{sp4}{ready} = _acc >= int({_compile_value(preset, ctx)})",
        f'{sp4}_mem["{frac_key}"] = _rem',
        f"{sp4}{done_write} = {ready}",
        f"{sp4}{acc_write} = _acc",
        f"{sp}else:",
        f"{sp4}{ready} = False",
        f'{sp4}_mem["{frac_key}"] = 0.0',
        f"{sp4}{done_write} = False",
        f"{sp4}{acc_write} = 0",
    ]


def _compile_debounce_instruction(
    instr: DebounceInstruction,
    enabled_expr: str,
    ctx: CodegenContext,
    indent: int,
) -> list[str]:
    _use_memory(ctx)
    sp = " " * indent
    target_write = _compile_lvalue(instr.target, ctx)
    return [
        f"{sp}_en = bool({enabled_expr})",
        *_compile_debounce_side(
            "_en", instr.on_done, instr.on_acc, instr.on_preset, "_on_ready", instr.unit, ctx, sp
        ),
        *_compile_debounce_side(
            "not _en",
            instr.off_done,
            instr.off_acc,
            instr.off_preset,
            "_off_ready",
            instr.unit,
            ctx,
            sp,
        ),
        f"{sp}if _on_ready:",
        f"{sp}    {target_write} = True",
        f"{sp}elif _off_ready:",
        f"{sp}    {target_write} = False",
    ]


def _compile_flasher_phase(
    instr: FlasherInstruction,
    acc: Tag,
    preset: Tag | int,
    other_acc: Tag,
    ends_on_phase: bool,
    ctx: CodegenContext,
    sp: str,
) -> list[str]:
    """One phase's tick; when it ends, the other phase starts."""
    sp4 = sp + "    "
    on_frac = f"_frac:{instr.on_acc.name}"
    off_frac = f"_frac:{instr.off_acc.name}"
    started_done, started_acc = (
        (instr.off_done, instr.off_acc) if ends_on_phase else (instr.on_done, instr.on_acc)
    )
    ended_done = instr.on_done if ends_on_phase else instr.off_done
    return [
        *_tick_lines(acc, instr.unit, ctx, sp),
        f"{sp}{_compile_lvalue(acc, ctx)} = _acc",
        f"{sp}if _acc >= int({_compile_value(preset, ctx)}):",
        f"{sp4}_off_phase = {ends_on_phase}",
        f"{sp4}{_compile_lvalue(ended_done, ctx)} = True",
        f"{sp4}{_compile_lvalue(started_done, ctx)} = False",
        f"{sp4}{_compile_lvalue(started_acc, ctx)} = 0",
        f'{sp4}_mem["{on_frac}"] = 0.0',
        f'{sp4}_mem["{off_frac}"] = 0.0',
        f"{sp}else:",
        f'{sp4}_mem["_frac:{acc.name}"] = _rem',
        _keep_frac_line(other_acc, sp4),
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
    return [
        f"{sp}if not bool({enabled_expr}):",
        f'{sp4}_mem["_frac:{instr.on_acc.name}"] = 0.0',
        f'{sp4}_mem["_frac:{instr.off_acc.name}"] = 0.0',
        f"{sp4}{_compile_lvalue(instr.target, ctx)} = False",
        f"{sp4}{_compile_lvalue(instr.on_done, ctx)} = False",
        f"{sp4}{_compile_lvalue(instr.on_acc, ctx)} = 0",
        f"{sp4}{_compile_lvalue(instr.off_done, ctx)} = False",
        f"{sp4}{_compile_lvalue(instr.off_acc, ctx)} = 0",
        f"{sp}else:",
        f"{sp4}_off_phase = bool({_compile_value(instr.on_done, ctx)})",
        f"{sp4}_first = (",
        f"{sp8}not _off_phase",
        f"{sp8}and not bool({_compile_value(instr.off_done, ctx)})",
        f"{sp8}and int({_compile_value(instr.on_acc, ctx)}) == 0",
        f'{sp8}and _mem.get("_frac:{instr.on_acc.name}", 0.0) == 0.0',
        f"{sp4})",
        f"{sp4}if not _off_phase:",
        *_compile_flasher_phase(
            instr, instr.on_acc, instr.on_preset, instr.off_acc, True, ctx, sp8
        ),
        f"{sp4}else:",
        *_compile_flasher_phase(
            instr, instr.off_acc, instr.off_preset, instr.on_acc, False, ctx, sp8
        ),
        f"{sp4}{_compile_lvalue(instr.target, ctx)} = _first or not _off_phase",
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
    target_write = _compile_lvalue(instr.target, ctx)
    done_write = _compile_lvalue(instr.timer_done, ctx)
    acc_write = _compile_lvalue(instr.timer_acc, ctx)
    timed_out_target = (
        "(_starting or _en or not _done)" if instr.maintained else ("(_starting or not _done)")
    )
    return [
        f"{sp}_en = bool({enabled_expr})",
        f"{sp}_was_done = bool({_compile_value(instr.timer_done, ctx)})",
        f"{sp}if _was_done and _en:",
        _keep_frac_line(instr.timer_acc, sp4),
        f"{sp4}{target_write} = {instr.maintained}",
        f"{sp}elif not _was_done and (_en or bool({_compile_value(instr.target, ctx)})):",
        f"{sp4}_starting = not bool({_compile_value(instr.target, ctx)})",
        *_tick_lines(instr.timer_acc, instr.unit, ctx, sp4),
        f"{sp4}_done = _acc >= int({_compile_value(instr.on_preset, ctx)})",
        f'{sp4}_mem["{frac_key}"] = _rem',
        f"{sp4}{target_write} = {timed_out_target}",
        f"{sp4}{done_write} = _done",
        f"{sp4}{acc_write} = _acc",
        f"{sp}else:",
        f'{sp4}_mem["{frac_key}"] = 0.0',
        f"{sp4}{target_write} = False",
        f"{sp4}{done_write} = False",
        f"{sp4}{acc_write} = 0",
    ]
