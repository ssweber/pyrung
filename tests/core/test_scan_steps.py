"""Tests for PLCRunner.scan_steps rung-level stepping."""

from __future__ import annotations

import pytest

from pyrung.core import (
    Block,
    Bool,
    Int,
    PLCRunner,
    Program,
    Rung,
    TagType,
    branch,
    call,
    count_down,
    count_up,
    copy,
    on_delay,
    out,
    return_,
    shift,
    subroutine,
)


def test_scan_steps_yields_each_rung_and_commits_at_exhaustion():
    start = Bool("Start")
    light1 = Bool("Light1")
    light2 = Bool("Light2")

    with Program(strict=False) as logic:
        with Rung(start):
            out(light1)
        with Rung(light1):
            out(light2)

    runner = PLCRunner(logic)
    runner.patch({"Start": True})

    scan_gen = runner.scan_steps()

    idx0, rung0, ctx0 = next(scan_gen)
    assert idx0 == 0
    assert rung0 is logic.rungs[0]
    assert ctx0.get_tag("Light1") is True
    assert runner.current_state.scan_id == 0
    assert "Light1" not in runner.current_state.tags

    idx1, rung1, ctx1 = next(scan_gen)
    assert idx1 == 1
    assert rung1 is logic.rungs[1]
    assert ctx1.get_tag("Light2") is True
    assert runner.current_state.scan_id == 0

    with pytest.raises(StopIteration):
        next(scan_gen)

    assert runner.current_state.scan_id == 1
    assert runner.current_state.tags["Light1"] is True
    assert runner.current_state.tags["Light2"] is True


def test_scan_steps_partial_consumption_does_not_commit_state():
    enable = Bool("Enable")
    output = Bool("Output")

    with Program(strict=False) as logic:
        with Rung(enable):
            out(output)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True})

    scan_gen = runner.scan_steps()
    _, _, ctx = next(scan_gen)

    assert runner.current_state.scan_id == 0
    assert "Output" not in runner.current_state.tags
    assert ctx.get_tag("Output") is True

    # Commit only happens once the generator is exhausted.
    for _ in scan_gen:
        pass

    assert runner.current_state.scan_id == 1
    assert runner.current_state.tags["Output"] is True


def test_scan_steps_debug_partial_consumption_does_not_commit_state():
    enable = Bool("Enable")
    output = Bool("Output")

    with Program(strict=False) as logic:
        with Rung(enable):
            out(output)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True})

    scan_gen = runner.scan_steps_debug()
    first_step = next(scan_gen)

    assert first_step.kind == "instruction"
    assert runner.current_state.scan_id == 0
    assert "Output" not in runner.current_state.tags

    second_step = next(scan_gen)
    assert second_step.kind == "rung"
    assert second_step.ctx.get_tag("Output") is True
    assert runner.current_state.scan_id == 0
    assert "Output" not in runner.current_state.tags

    # Commit only happens once the generator is exhausted.
    for _ in scan_gen:
        pass

    assert runner.current_state.scan_id == 1
    assert runner.current_state.tags["Output"] is True


def test_step_and_scan_steps_have_equivalent_results():
    enable = Bool("Enable")
    light = Bool("Light")

    with Program(strict=False) as logic:
        with Rung(enable):
            out(light)

    via_step = PLCRunner(logic)
    via_scan_steps = PLCRunner(logic)

    via_step.patch({"Enable": True})
    via_scan_steps.patch({"Enable": True})

    via_step.step()
    for _ in via_scan_steps.scan_steps():
        pass

    assert via_step.current_state == via_scan_steps.current_state


def test_scan_steps_debug_yields_subroutine_branch_and_top_rung():
    sub_light = Bool("SubLight")
    branch_light = Bool("BranchLight")
    top_light = Bool("TopLight")

    with Program(strict=False) as logic:
        with subroutine("init_sub"):
            with Rung():
                out(sub_light)

        with Rung():
            call("init_sub")
            with branch():
                out(branch_light)
            out(top_light)

    runner = PLCRunner(logic)
    steps = list(runner.scan_steps_debug())

    boundary_steps = [step for step in steps if step.kind != "instruction"]
    assert [step.kind for step in boundary_steps] == ["subroutine", "branch", "rung"]
    assert [step.depth for step in boundary_steps] == [1, 1, 0]
    assert [step.rung_index for step in boundary_steps] == [0, 0, 0]
    assert boundary_steps[0].subroutine_name == "init_sub"
    assert boundary_steps[0].call_stack == ("init_sub",)
    assert boundary_steps[1].call_stack == ()
    assert boundary_steps[2].call_stack == ()
    assert runner.current_state.tags["SubLight"] is True
    assert runner.current_state.tags["BranchLight"] is True
    assert runner.current_state.tags["TopLight"] is True


def test_scan_steps_debug_handles_return_signal_and_still_yields_return_rung():
    first = Bool("First")
    skipped = Bool("Skipped")
    done = Bool("Done")

    with Program(strict=False) as logic:
        with subroutine("work"):
            with Rung():
                out(first)
            with Rung():
                return_()
            with Rung():
                out(skipped)

        with Rung():
            call("work")
            out(done)

    runner = PLCRunner(logic)
    steps = list(runner.scan_steps_debug())

    sub_steps = [step for step in steps if step.kind == "subroutine"]
    assert len(sub_steps) == 2
    assert all(step.subroutine_name == "work" for step in sub_steps)
    assert runner.current_state.tags["First"] is True
    assert "Skipped" not in runner.current_state.tags
    assert runner.current_state.tags["Done"] is True


def test_scan_steps_debug_does_not_yield_unpowered_branch():
    enable = Bool("Enable")
    branch_out = Bool("BranchOut")
    top_out = Bool("TopOut")

    with Program(strict=False) as logic:
        with Rung(enable):
            with branch():
                out(branch_out)
            out(top_out)

    runner = PLCRunner(logic)
    runner.patch({"Enable": False})
    steps = list(runner.scan_steps_debug())

    kinds = [step.kind for step in steps]
    assert [kind for kind in kinds if kind != "instruction"] == ["rung"]
    assert runner.current_state.tags["BranchOut"] is False


def test_scan_steps_debug_respects_source_order_branch_before_call():
    step = Int("Step")
    auto = Bool("Auto")
    branch_done = Bool("BranchDone")
    sub_light = Bool("SubLight")

    with Program(strict=False) as logic:
        with subroutine("sub"):
            with Rung(step == 1):
                out(sub_light)

        with Rung(step == 0):
            with branch(auto):
                out(branch_done)
                copy(1, step, oneshot=True)
            call("sub")

    runner = PLCRunner(logic)
    runner.patch({"Step": 0, "Auto": True, "BranchDone": False, "SubLight": False})
    steps = list(runner.scan_steps_debug())

    kinds = [entry.kind for entry in steps]
    assert [kind for kind in kinds if kind != "instruction"] == ["branch", "subroutine", "rung"]
    assert runner.current_state.tags["Step"] == 1
    assert runner.current_state.tags["BranchDone"] is True
    assert runner.current_state.tags["SubLight"] is True


def test_scan_steps_debug_uses_precomputed_branch_enable():
    enable = Bool("Enable")
    mode = Bool("Mode")
    branch_out = Bool("BranchOut")

    with Program(strict=False) as logic:
        with Rung(enable):
            copy(True, mode)
            with branch(mode):
                out(branch_out)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Mode": False, "BranchOut": False})
    steps = list(runner.scan_steps_debug())

    # Branch remains unpowered for this scan despite Mode being written before branch item.
    kinds = [step.kind for step in steps]
    assert [kind for kind in kinds if kind != "instruction"] == ["rung"]
    assert runner.current_state.tags["Mode"] is True
    assert runner.current_state.tags["BranchOut"] is False


def test_scan_steps_debug_emits_chained_builder_substeps_with_substep_only_trace():
    enable = Bool("Enable")
    down = Bool("Down")
    reset = Bool("Reset")
    clock = Bool("Clock")
    up_done = Bool("UpDone")
    up_acc = Int("UpAcc")
    down_done = Bool("DownDone")
    down_acc = Int("DownAcc")
    timer_done = Bool("TimerDone")
    timer_acc = Int("TimerAcc")
    bits = Block("C", TagType.BOOL, 1, 8)

    with Program(strict=False) as logic:
        with Rung(enable):
            count_up(up_done, up_acc, setpoint=5).down(down).reset(reset)

        with Rung(enable):
            count_down(down_done, down_acc, setpoint=5).reset(reset)

        with Rung(enable):
            on_delay(timer_done, timer_acc, setpoint=50).reset(reset)

        with Rung(enable):
            shift(bits.select(1, 4)).clock(clock).reset(reset)

    runner = PLCRunner(logic)
    runner.patch({"Enable": True, "Down": True, "Reset": False, "Clock": True})
    instruction_steps = [step for step in runner.scan_steps_debug() if step.kind == "instruction"]

    assert [step.instruction_kind for step in instruction_steps] == [
        "Count Up",
        "Count Down",
        "Reset",
        "Count Down",
        "Reset",
        "Enable",
        "Reset",
        "Data",
        "Clock",
        "Reset",
    ]

    for step in instruction_steps:
        assert step.trace is not None
        regions = step.trace["regions"]
        assert len(regions) == 1
        region = regions[0]
        assert region["kind"] == "instruction"
        assert region["source_line"] == step.source_line
        assert region["end_line"] == step.source_line
        conditions = region["conditions"]
        assert len(conditions) == 1
        condition = conditions[0]
        assert condition["source_line"] == step.source_line
        assert isinstance(condition["expression"], str) and condition["expression"]
