"""Tests for source-file/line metadata capture on DSL elements."""

from __future__ import annotations

import inspect
from pathlib import Path
from types import FrameType
from typing import cast

from pyrung.core import (
    Block,
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    TagType,
    all_of,
    any_of,
    branch,
    copy,
    count_down,
    count_up,
    forloop,
    latch,
    nc,
    on_delay,
    out,
    rise,
    shift,
)
from pyrung.core.instruction import ForLoopInstruction


def _line_no() -> int:
    frame = cast(FrameType, inspect.currentframe())
    caller = cast(FrameType, frame.f_back)
    return caller.f_lineno


def test_rung_captures_source_file_start_line_and_end_line():
    enable = Bool("Enable")
    light = Bool("Light")
    latched = Bool("Latched")

    with Program(strict=False) as prog:
        rung_line = _line_no() + 1
        with Rung(enable):
            out_line = _line_no() + 1
            out(light)
            end_line = _line_no() + 1
            latch(latched)

    rung = prog.rungs[0]
    assert rung.source_file is not None
    assert Path(rung.source_file).name == Path(__file__).name
    assert rung.source_line == rung_line
    assert rung.end_line == end_line
    assert rung._instructions[0].source_line == out_line


def test_condition_helpers_and_operators_capture_source_lines():
    a = Bool("A")
    b = Bool("B")
    c = Bool("C")
    step = Int("Step")

    line_nc = _line_no() + 1
    cond_nc = nc(a)
    line_rise = _line_no() + 1
    cond_rise = rise(b)
    line_any = _line_no() + 1
    cond_any = any_of(a, b)
    line_all = _line_no() + 1
    cond_all = all_of(a, c)
    line_or = _line_no() + 1
    cond_or = a | b
    line_and = _line_no() + 1
    cond_and = a & b
    line_cmp = _line_no() + 1
    cond_cmp = step == 10
    line_expr_cmp = _line_no() + 1
    cond_expr_cmp = (step + 1) > 0
    line_chain = _line_no() + 1
    cond_chain = (a | b) | (step > 1)

    assert cond_nc.source_line == line_nc
    assert cond_rise.source_line == line_rise
    assert cond_any.source_line == line_any
    assert cond_all.source_line == line_all
    assert cond_or.source_line == line_or
    assert cond_and.source_line == line_and
    assert cond_cmp.source_line == line_cmp
    assert cond_expr_cmp.source_line == line_expr_cmp
    assert cond_chain.source_line == line_chain

    # Direct-Tag children get inherited source metadata when they are wrapped.
    assert cond_or.conditions[0].source_line == line_or
    assert cond_or.conditions[1].source_line == line_or


def test_builder_paths_capture_source_lines_for_branch_forloop_and_terminal_instructions():
    enable = Bool("Enable")
    branch_enable = Bool("BranchEnable")
    branch_out = Bool("BranchOut")
    forloop_counter = Int("ForLoopCounter")
    reset_cond = Bool("Reset")
    cu_done = Bool("CountUpDone")
    cu_acc = Int("CountUpAcc")
    cd_done = Bool("CountDownDone")
    cd_acc = Int("CountDownAcc")
    timer_done = Bool("TimerDone")
    timer_acc = Int("TimerAcc")
    clock = Bool("Clock")
    bits = Block("C", TagType.BOOL, 1, 8)

    with Program(strict=False) as prog:
        with Rung(enable):
            branch_line = _line_no() + 1
            with branch(branch_enable):
                branch_end_line = _line_no() + 1
                out(branch_out)

        with Rung(enable):
            forloop_line = _line_no() + 1
            with forloop(2):
                forloop_copy_line = _line_no() + 1
                copy(forloop_counter + 1, forloop_counter)

        with Rung(enable):
            count_up_line = _line_no() + 1
            count_up(cu_done, cu_acc, setpoint=5).reset(reset_cond)

        with Rung(enable):
            count_down_line = _line_no() + 1
            count_down(cd_done, cd_acc, setpoint=5).reset(reset_cond)

        with Rung(enable):
            on_delay_line = _line_no() + 1
            on_delay(timer_done, timer_acc, setpoint=50).reset(reset_cond)

        with Rung(enable):
            shift_line = _line_no() + 1
            shift(bits.select(1, 4)).clock(clock).reset(reset_cond)

    branch_rung = prog.rungs[0]._branches[0]
    assert branch_rung.source_line == branch_line
    assert branch_rung.end_line == branch_end_line

    forloop_instr = prog.rungs[1]._instructions[0]
    assert isinstance(forloop_instr, ForLoopInstruction)
    assert forloop_instr.source_line == forloop_line
    assert forloop_instr.instructions[0].source_line == forloop_copy_line

    count_up_instr = prog.rungs[2]._instructions[0]
    assert type(count_up_instr).__name__ == "CountUpInstruction"
    assert count_up_instr.source_line == count_up_line

    count_down_instr = prog.rungs[3]._instructions[0]
    assert type(count_down_instr).__name__ == "CountDownInstruction"
    assert count_down_instr.source_line == count_down_line

    on_delay_instr = prog.rungs[4]._instructions[0]
    assert type(on_delay_instr).__name__ == "OnDelayInstruction"
    assert on_delay_instr.source_line == on_delay_line

    shift_instr = prog.rungs[5]._instructions[0]
    assert type(shift_instr).__name__ == "ShiftInstruction"
    assert shift_instr.source_line == shift_line


def test_multiline_rung_direct_tag_condition_uses_argument_line_number():
    step = Int("Step")
    auto_mode = Bool("AutoMode")
    light = Bool("Light")

    with Program(strict=False) as prog:
        marker_line = _line_no()
        with Rung(
            step == 1,
            auto_mode,
        ):
            out(light)

    step_condition_line = marker_line + 2
    auto_condition_line = marker_line + 3
    rung = prog.rungs[0]
    assert rung._conditions[0].source_line == step_condition_line
    assert rung._conditions[1].source_line == auto_condition_line


def test_multiline_count_up_captures_instruction_end_line_and_debug_step_end_line():
    enable = Bool("Enable")
    done = Bool("Done")
    acc = Int("Acc")
    reset_cond = Bool("Reset")

    with Program(strict=False) as prog:
        with Rung(enable):
            count_up_line = _line_no() + 1
            count_up(
                done,
                acc,
                setpoint=5,
            ).reset(reset_cond)
            count_up_end_line = _line_no() - 1

    instruction = prog.rungs[0]._instructions[0]
    assert instruction.source_line == count_up_line
    assert instruction.end_line == count_up_end_line

    runner = PLCRunner(prog)
    runner.patch({"Enable": True, "Reset": False})
    step = next(runner.scan_steps_debug())
    assert step.kind == "instruction"
    assert step.source_line == count_up_line
    assert step.end_line == count_up_line


def test_chained_builder_methods_capture_distinct_debug_substep_lines():
    enable = Bool("Enable")
    down = Bool("Down")
    reset_cond = Bool("Reset")
    clock = Bool("Clock")
    up_done = Bool("UpDone")
    up_acc = Int("UpAcc")
    down_done = Bool("DownDone")
    down_acc = Int("DownAcc")
    timer_done = Bool("TimerDone")
    timer_acc = Int("TimerAcc")
    bits = Block("C", TagType.BOOL, 1, 8)

    with Program(strict=False) as prog:
        with Rung(enable):
            cu_line = _line_no() + 1
            cu_builder = count_up(up_done, up_acc, setpoint=5)
            cu_down_line = _line_no() + 1
            cu_builder = cu_builder.down(down)
            cu_reset_line = _line_no() + 1
            cu_builder.reset(reset_cond)

        with Rung(enable):
            cd_line = _line_no() + 1
            cd_builder = count_down(down_done, down_acc, setpoint=5)
            cd_reset_line = _line_no() + 1
            cd_builder.reset(reset_cond)

        with Rung(enable):
            timer_line = _line_no() + 1
            timer_builder = on_delay(timer_done, timer_acc, setpoint=50)
            timer_reset_line = _line_no() + 1
            timer_builder.reset(reset_cond)

        with Rung(enable):
            shift_line = _line_no() + 1
            shift_builder = shift(bits.select(1, 4))
            shift_clock_line = _line_no() + 1
            shift_builder = shift_builder.clock(clock)
            shift_reset_line = _line_no() + 1
            shift_builder.reset(reset_cond)

    cu_instr = prog.rungs[0]._instructions[0]
    assert cu_instr.debug_substeps is not None
    assert [step.instruction_kind for step in cu_instr.debug_substeps] == [
        "Count Up",
        "Count Down",
        "Reset",
    ]
    assert [step.source_line for step in cu_instr.debug_substeps] == [
        cu_line,
        cu_down_line,
        cu_reset_line,
    ]

    cd_instr = prog.rungs[1]._instructions[0]
    assert cd_instr.debug_substeps is not None
    assert [step.instruction_kind for step in cd_instr.debug_substeps] == [
        "Count Down",
        "Reset",
    ]
    assert [step.source_line for step in cd_instr.debug_substeps] == [
        cd_line,
        cd_reset_line,
    ]

    timer_instr = prog.rungs[2]._instructions[0]
    assert timer_instr.debug_substeps is not None
    assert [step.instruction_kind for step in timer_instr.debug_substeps] == [
        "Enable",
        "Reset",
    ]
    assert [step.source_line for step in timer_instr.debug_substeps] == [
        timer_line,
        timer_reset_line,
    ]

    shift_instr = prog.rungs[3]._instructions[0]
    assert shift_instr.debug_substeps is not None
    assert [step.instruction_kind for step in shift_instr.debug_substeps] == [
        "Data",
        "Clock",
        "Reset",
    ]
    assert [step.source_line for step in shift_instr.debug_substeps] == [
        shift_line,
        shift_clock_line,
        shift_reset_line,
    ]

    runner = PLCRunner(prog)
    runner.patch({"Enable": True, "Down": True, "Reset": False, "Clock": True})
    instruction_steps = [step for step in runner.scan_steps_debug() if step.kind == "instruction"]
    assert [step.source_line for step in instruction_steps] == [
        cu_line,
        cu_down_line,
        cu_reset_line,
        cd_line,
        cd_reset_line,
        timer_line,
        timer_reset_line,
        shift_line,
        shift_clock_line,
        shift_reset_line,
    ]
