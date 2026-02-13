"""Tests for custom() DSL escape-hatch instruction."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, custom


def test_custom_executes_when_rung_true():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(ctx):
        ctx.set_tag(Output.name, 7)

    with Program() as logic:
        with Rung(Enable):
            custom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()

    assert runner.current_state.tags["Output"] == 7


def test_custom_skipped_when_rung_false():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(ctx):
        ctx.set_tag(Output.name, 99)

    with Program() as logic:
        with Rung(Enable):
            custom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Output": 3})
    runner.step()

    assert runner.current_state.tags["Output"] == 3


def test_custom_oneshot_fires_once_per_activation():
    Enable = Bool("Enable")
    Count = Int("Count")

    def callback(ctx):
        current = int(ctx.get_tag(Count.name, 0))
        ctx.set_tag(Count.name, current + 1)

    with Program() as logic:
        with Rung(Enable):
            custom(callback, oneshot=True)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Count": 0})
    runner.step()
    assert runner.current_state.tags["Count"] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Count"] == 1

    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Count"] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Count"] == 2


def test_custom_reads_and_writes_tags():
    Enable = Bool("Enable")
    A = Int("A")
    B = Int("B")
    Result = Int("Result")

    def callback(ctx):
        a = int(ctx.get_tag(A.name, 0))
        b = int(ctx.get_tag(B.name, 0))
        ctx.set_tag(Result.name, a * 2 + b)

    with Program() as logic:
        with Rung(Enable):
            custom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "A": 10, "B": 5, "Result": 0})
    runner.step()

    assert runner.current_state.tags["Result"] == 25


def test_custom_with_memory():
    Enable = Bool("Enable")
    Counter = Int("Counter")
    key = "_custom:test:counter"

    def callback(ctx):
        current = int(ctx.get_memory(key, 0))
        next_value = current + 1
        ctx.set_memory(key, next_value)
        ctx.set_tag(Counter.name, next_value)

    with Program() as logic:
        with Rung(Enable):
            custom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Counter": 0})
    runner.step()
    assert runner.current_state.tags["Counter"] == 1
    assert runner.current_state.memory[key] == 1

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Counter"] == 2
    assert runner.current_state.memory[key] == 2


def test_custom_outside_rung_raises_runtime_error():
    with pytest.raises(RuntimeError) as exc_info:
        custom(lambda ctx: None)
    assert str(exc_info.value) == "custom() must be called inside a Rung context"


def test_custom_non_callable_raises_type_error():
    Enable = Bool("Enable")
    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                custom(123)  # type: ignore[arg-type]
    assert str(exc_info.value) == "custom() fn must be callable, got int"


def test_custom_wrong_arity_rejected():
    Enable = Bool("Enable")

    def invalid_callback(ctx, required2):
        _ = (ctx, required2)

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                custom(invalid_callback)
    assert str(exc_info.value) == "custom() expects callable compatible with (ctx)"


def test_custom_keyword_only_required_rejected():
    Enable = Bool("Enable")

    def invalid_callback(*, ctx):
        _ = ctx

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                custom(invalid_callback)
    assert str(exc_info.value) == "custom() expects callable compatible with (ctx)"


def test_custom_async_callback_rejected():
    Enable = Bool("Enable")

    async def invalid_callback(ctx):
        _ = ctx

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                custom(invalid_callback)
    assert str(exc_info.value) == "custom() callback must be synchronous (async def is not supported)"


def test_custom_varargs_signature_accepted():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(ctx, *args):
        _ = args
        ctx.set_tag(Output.name, 1)

    with Program() as logic:
        with Rung(Enable):
            custom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": True, "Output": 0})
    runner.step()
    assert runner.current_state.tags["Output"] == 1
