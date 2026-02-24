"""Tests for the simple task sequencer example."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def simple_task(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
    module_name = "examples.simple_task_example"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_call_activates_and_enters_step_1(simple_task: ModuleType) -> None:
    runner = simple_task.runner

    with runner.active():
        simple_task.Task.Call.value = 1

    runner.step()

    with runner.active():
        assert simple_task.Task.Active.value == 1
        assert simple_task.Task.Step.value == 1


def test_step_timer_advances_after_5_seconds(simple_task: ModuleType) -> None:
    runner = simple_task.runner

    with runner.active():
        simple_task.Task.Call.value = 1

    runner.step()  # Enter step 1 and set Active.
    runner.run(cycles=500)  # Branch sees StepTime>=5 one scan after timer writes it.

    with runner.active():
        assert simple_task.Task.Step.value == 2
        assert simple_task.Task.StepTime.value == 0


def test_auto_reset_when_call_cleared(simple_task: ModuleType) -> None:
    runner = simple_task.runner

    with runner.active():
        simple_task.Task.Call.value = 1

    runner.step()
    runner.run(cycles=200)

    with runner.active():
        assert simple_task.Task.Active.value == 1
        assert simple_task.Task.Step.value == 1
        assert simple_task.Valve1.value is True
        simple_task.Task.Call.value = 0

    runner.step()  # Reset rung clears Step/Active/Advance/StepTime.
    runner.step()  # out(Valve1) branch sees Step!=1 and auto-resets.

    with runner.active():
        assert simple_task.Task.Active.value == 0
        assert simple_task.Task.Step.value == 0
        assert simple_task.Task.Advance.value == 0
        assert simple_task.Task.StepTime.value == 0
        assert simple_task.Valve1.value is False


def test_valve_energized_during_step_1(simple_task: ModuleType) -> None:
    runner = simple_task.runner

    with runner.active():
        simple_task.Task.Call.value = 1

    runner.step()
    runner.step()

    with runner.active():
        assert simple_task.Valve1.value is True
