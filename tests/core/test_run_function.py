"""Tests for run_function() DSL instruction."""

from __future__ import annotations

import pytest

from pyrung.core import Block, Bool, Int, PLCRunner, Program, Rung, TagType, run_function


def test_run_function_copy_in_execute_copy_out():
    Enable = Bool("Enable")
    SensorA = Int("SensorA")
    SensorB = Int("SensorB")
    Average = Int("Average")

    def weighted_average(temp, pressure):
        return {"result": (temp + pressure) / 2}

    with Program() as logic:
        with Rung(Enable):
            run_function(
                weighted_average,
                ins={"temp": SensorA, "pressure": SensorB},
                outs={"result": Average},
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "SensorA": 10, "SensorB": 6, "Average": 0})
    runner.step()

    assert runner.current_state.tags["Average"] == 8


def test_run_function_skipped_when_rung_false():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback():
        return {"out": 99}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Output": 3})
    runner.step()

    assert runner.current_state.tags["Output"] == 3


def test_run_function_accepts_literal_inputs():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(a, b):
        return {"out": a + b}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, ins={"a": 10, "b": 32}, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 42


def test_run_function_accepts_mixed_tag_and_literal_inputs():
    Enable = Bool("Enable")
    A = Int("A")
    Output = Int("Output")

    def callback(a, b):
        return {"out": a + b}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, ins={"a": A, "b": 5}, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "A": 7, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 12


def test_run_function_oneshot_fires_once_per_activation():
    Enable = Bool("Enable")
    Output = Int("Output")
    calls = 0

    def callback():
        nonlocal calls
        calls += 1
        return {"out": calls}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output}, oneshot=True)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()
    assert runner.current_state.tags["Output"] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Output"] == 1

    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Output"] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Output"] == 2


def test_run_function_ins_none_for_noarg_function():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback():
        return {"out": 11}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 11


def test_run_function_outs_none_discards_return_value():
    Enable = Bool("Enable")
    Calls = Int("Calls")

    def callback():
        return {"ignored": 123}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback)
            run_function(lambda: {"calls": 1}, outs={"calls": Calls})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Calls": 0})
    runner.step()

    assert runner.current_state.tags["Calls"] == 1


def test_run_function_output_type_coercion_clamps_int():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback():
        return {"out": 70000}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 32767


def test_run_function_missing_output_key_raises_key_error():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback():
        return {"other": 1}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})

    with pytest.raises(KeyError, match="run_function"):
        runner.step()


def test_run_function_extra_output_keys_are_ignored():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback():
        return {"out": 12, "unused": 99}

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 12


def test_run_function_returning_none_with_outs_raises_type_error():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback():
        return None

    with Program() as logic:
        with Rung(Enable):
            run_function(callback, outs={"out": Output})

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})

    with pytest.raises(TypeError, match="run_function"):
        runner.step()


def test_run_function_outside_rung_raises_runtime_error():
    with pytest.raises(RuntimeError) as exc_info:
        run_function(lambda: {"ok": 1})
    assert str(exc_info.value) == "run_function() must be called inside a Rung context"


def test_run_function_non_callable_raises_type_error():
    Enable = Bool("Enable")
    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_function(123)  # type: ignore[arg-type]
    assert str(exc_info.value) == "run_function() fn must be callable, got int"


def test_run_function_async_function_rejected():
    Enable = Bool("Enable")

    async def callback():
        return {"ok": True}

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_function(callback)
    assert (
        str(exc_info.value)
        == "run_function() fn must be synchronous (async def is not supported)"
    )


def test_run_function_ins_keys_must_match_function_signature():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(a):
        return {"out": a}

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_function(callback, ins={"a": 1, "b": 2}, outs={"out": Output})
    assert "run_function() ins keys" in str(exc_info.value)


def test_run_function_ins_must_be_dict():
    Enable = Bool("Enable")

    def callback(a):
        return {"out": a}

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                run_function(callback, ins=1)  # type: ignore[arg-type]
    assert str(exc_info.value) == "run_function() ins must be a dict, got int"


def test_run_function_supports_expression_and_indirectref_in_inputs():
    Enable = Bool("Enable")
    DS = Block("DS", TagType.INT, 1, 100)
    Index = Int("Index")
    Output = Int("Output")

    def callback(value, expr):
        return {"out": value + expr}

    with Program() as logic:
        with Rung(Enable):
            run_function(
                callback,
                ins={"value": DS[Index], "expr": DS[1] * 2},
                outs={"out": Output},
            )

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Index": 2, "DS1": 3, "DS2": 10, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 16


def test_run_function_function_exception_propagates():
    Enable = Bool("Enable")

    def callback():
        raise ValueError("boom")

    with Program() as logic:
        with Rung(Enable):
            run_function(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True})

    with pytest.raises(ValueError, match="boom"):
        runner.step()
