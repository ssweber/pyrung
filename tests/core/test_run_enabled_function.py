"""Tests for run_enabled_function() DSL instruction."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, run_enabled_function


def test_run_enabled_function_invoked_every_scan_with_enabled_transitions():
    Enable = Bool("Enable")
    Calls = Int("Calls")
    EnabledSeen = Bool("EnabledSeen")
    seen: list[bool] = []

    def callback(enabled):
        seen.append(enabled)
        return {"calls": len(seen), "enabled": enabled}

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(callback, outs={"calls": Calls, "enabled": EnabledSeen})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Calls": 0, "EnabledSeen": False})
    runner.step()
    runner.patch({"Enable": True})
    runner.step()
    runner.patch({"Enable": False})
    runner.step()

    assert seen == [False, True, False]
    assert runner.current_state.tags["Calls"] == 3
    assert runner.current_state.tags["EnabledSeen"] is False


def test_run_enabled_function_receives_resolved_kwargs():
    Enable = Bool("Enable")
    A = Int("A")
    B = Int("B")
    Sum = Int("Sum")

    def callback(enabled, a, b):
        if not enabled:
            return {"sum": -1}
        return {"sum": a + b}

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(callback, ins={"a": A, "b": B}, outs={"sum": Sum})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "A": 12, "B": 30, "Sum": 0})
    runner.step()
    assert runner.current_state.tags["Sum"] == 42


def test_run_enabled_function_runs_when_rung_false():
    Enable = Bool("Enable")
    Calls = Int("Calls")

    def callback(enabled):
        return {"calls": 1 if enabled else 2}

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(callback, outs={"calls": Calls})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Calls": 0})
    runner.step()

    assert runner.current_state.tags["Calls"] == 2


def test_run_enabled_function_class_state_persists_across_scans():
    Enable = Bool("Enable")
    Value = Int("Value")
    Total = Int("Total")

    class Accumulator:
        def __init__(self):
            self.total = 0

        def __call__(self, enabled, value):
            if enabled:
                self.total += int(value)
            return {"total": self.total}

    acc = Accumulator()

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(acc, ins={"value": Value}, outs={"total": Total})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Value": 2, "Total": 0})
    runner.step()
    assert runner.current_state.tags["Total"] == 2

    runner.patch({"Enable": True, "Value": 3})
    runner.step()
    assert runner.current_state.tags["Total"] == 5

    runner.patch({"Enable": False, "Value": 9})
    runner.step()
    assert runner.current_state.tags["Total"] == 5


def test_run_enabled_function_writes_multiple_output_tags():
    Enable = Bool("Enable")
    Sending = Bool("Sending")
    Success = Bool("Success")
    Error = Bool("Error")

    def callback(enabled):
        return {"sending": enabled, "success": False, "error": not enabled}

    with Program() as logic:
        with Rung(Enable):
            run_enabled_function(
                callback,
                outs={"sending": Sending, "success": Success, "error": Error},
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Sending": False, "Success": False, "Error": False})
    runner.step()

    assert runner.current_state.tags["Sending"] is False
    assert runner.current_state.tags["Success"] is False
    assert runner.current_state.tags["Error"] is True


def test_run_enabled_function_outside_rung_raises_runtime_error():
    with pytest.raises(RuntimeError) as exc_info:
        run_enabled_function(lambda enabled: {"ok": enabled})
    assert str(exc_info.value) == "run_enabled_function() must be called inside a Rung context"


def test_run_enabled_function_non_callable_raises_type_error():
    Enable = Bool("Enable")
    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_enabled_function(123)  # type: ignore[arg-type]
    assert str(exc_info.value) == "run_enabled_function() fn must be callable, got int"


def test_run_enabled_function_async_function_rejected():
    Enable = Bool("Enable")

    async def callback(enabled):
        return {"ok": enabled}

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_enabled_function(callback)
    assert (
        str(exc_info.value)
        == "run_enabled_function() fn must be synchronous (async def is not supported)"
    )


def test_run_enabled_function_requires_enabled_arg_in_signature():
    Enable = Bool("Enable")

    def callback():
        return {"ok": True}

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_enabled_function(callback)
    assert "run_enabled_function() ins keys" in str(exc_info.value)


def test_run_enabled_function_ins_keys_must_match_signature():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(enabled, a):
        _ = enabled
        return {"out": a}

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_enabled_function(callback, ins={"a": 1, "b": 2}, outs={"out": Output})
    assert "run_enabled_function() ins keys" in str(exc_info.value)
