"""Tests for the task sequencer example."""

from __future__ import annotations

import importlib
import sys
from types import ModuleType

import pytest


@pytest.fixture
def task_example(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.setenv("PYRUNG_DAP_ACTIVE", "1")
    module_name = "examples.task_example"
    if module_name in sys.modules:
        return importlib.reload(sys.modules[module_name])
    return importlib.import_module(module_name)


def test_call_activates_task_and_enters_step_1(task_example: ModuleType) -> None:
    runner = task_example.runner

    with runner.active():
        task_example.Task.Call.value = 1

    runner.step()

    with runner.active():
        assert task_example.Task.Active.value == 1
        assert task_example.Task.Step.value == 1


def test_step_timer_reaches_5s_then_transitions_to_steps_2_and_3(
    task_example: ModuleType,
) -> None:
    runner = task_example.runner

    with runner.active():
        task_example.Task.Call.value = 1

    runner.step()  # Enter step 1 and set Active.
    runner.run(cycles=499)  # 5.0 seconds at 10ms fixed step.

    with runner.active():
        assert task_example.Task.Elapsed.value == 5
        assert task_example.Task.Step.value == 2
        assert task_example.Task.StepTime.value == 0

    runner.step()

    with runner.active():
        assert task_example.Task.Step.value == 3


def test_pause_resets_valve_and_returns_early(task_example: ModuleType) -> None:
    runner = task_example.runner

    with runner.active():
        task_example.Task.Call.value = 1

    runner.step()
    runner.step()  # Energize Valve1 while step 1 is active.

    with runner.active():
        assert task_example.Valve1.value is True
        task_example.Task.Pause.value = 1
        task_example.Task.Call.value = 0

    runner.step()

    with runner.active():
        assert task_example.Valve1.value is False
        assert task_example.Task.Active.value == 1
        assert task_example.Task.Step.value == 1


def test_call_zero_clears_all_task_state(task_example: ModuleType) -> None:
    runner = task_example.runner

    with runner.active():
        task_example.Task.Call.value = 1

    runner.step()
    runner.run(cycles=200)

    with runner.active():
        assert task_example.Task.Active.value == 1
        assert task_example.Task.Step.value == 1
        assert task_example.Task.StepTime.value >= 1
        assert task_example.Valve1.value is True
        task_example.Task.Pause.value = 0
        task_example.Task.Call.value = 0

    runner.step()

    with runner.active():
        assert task_example.Task.Active.value == 0
        assert task_example.Task.Step.value == 0
        assert task_example.Task.Advance.value == 0
        assert task_example.Task.Elapsed.value == 0
        assert task_example.Task.StepTime.value == 0
        assert task_example.Valve1.value is False
        assert task_example.Step1_Active.value is False
