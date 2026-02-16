"""Tests for PLCRunner.scan_steps rung-level stepping."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, PLCRunner, Program, Rung, out


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

