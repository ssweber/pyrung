"""Tests for acustom() DSL escape-hatch instruction."""

from __future__ import annotations

import pytest

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, acustom


def test_acustom_invoked_every_scan_with_enabled_transitions():
    Enable = Bool("Enable")
    Calls = Int("Calls")
    EnabledSeen = Bool("EnabledSeen")
    seen: list[bool] = []

    def callback(ctx, enabled):
        seen.append(enabled)
        current = int(ctx.get_tag(Calls.name, 0))
        ctx.set_tag(Calls.name, current + 1)
        ctx.set_tag(EnabledSeen.name, enabled)

    with Program() as logic:
        with Rung(Enable):
            acustom(callback)

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


def test_acustom_with_memory_state_across_true_false_scans():
    Enable = Bool("Enable")
    Phase = Int("Phase")
    pending_key = "_custom:test:pending"

    def callback(ctx, enabled):
        pending = bool(ctx.get_memory(pending_key, False))
        if enabled and not pending:
            ctx.set_memory(pending_key, True)
            ctx.set_tag(Phase.name, 1)
            return
        if enabled and pending:
            ctx.set_tag(Phase.name, 2)
            return
        if not enabled and pending:
            ctx.set_memory(pending_key, False)
            ctx.set_tag(Phase.name, 3)
            return
        ctx.set_tag(Phase.name, 0)

    with Program() as logic:
        with Rung(Enable):
            acustom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Phase": 0})
    runner.step()
    assert runner.current_state.tags["Phase"] == 0

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Phase"] == 1
    assert runner.current_state.memory[pending_key] is True

    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Phase"] == 2
    assert runner.current_state.memory[pending_key] is True

    runner.patch({"Enable": False})
    runner.step()
    assert runner.current_state.tags["Phase"] == 3
    assert runner.current_state.memory[pending_key] is False


def test_acustom_outside_rung_raises_runtime_error():
    with pytest.raises(RuntimeError) as exc_info:
        acustom(lambda ctx, enabled: None)
    assert str(exc_info.value) == "acustom() must be called inside a Rung context"


def test_acustom_non_callable_raises_type_error():
    Enable = Bool("Enable")
    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                acustom(123)  # type: ignore[arg-type]
    assert str(exc_info.value) == "acustom() fn must be callable, got int"


def test_acustom_missing_enabled_arg_rejected():
    Enable = Bool("Enable")

    def invalid_callback(ctx):
        _ = ctx

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                acustom(invalid_callback)
    assert str(exc_info.value) == "acustom() expects callable compatible with (ctx, enabled)"


def test_acustom_keyword_only_required_rejected():
    Enable = Bool("Enable")

    def invalid_callback(ctx, *, enabled):
        _ = (ctx, enabled)

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                acustom(invalid_callback)
    assert str(exc_info.value) == "acustom() expects callable compatible with (ctx, enabled)"


def test_acustom_async_callback_rejected():
    Enable = Bool("Enable")

    async def invalid_callback(ctx, enabled):
        _ = (ctx, enabled)

    with pytest.raises(TypeError) as exc_info:
        with Program():
            with Rung(Enable):
                acustom(invalid_callback)
    assert str(exc_info.value) == "acustom() callback must be synchronous (async def is not supported)"


def test_acustom_varargs_signature_accepted():
    Enable = Bool("Enable")
    Output = Int("Output")

    def callback(ctx, enabled, *args):
        _ = (enabled, args)
        current = int(ctx.get_tag(Output.name, 0))
        ctx.set_tag(Output.name, current + 1)

    with Program() as logic:
        with Rung(Enable):
            acustom(callback)

    runner = PLCRunner(logic=logic)
    runner.patch({"Enable": False, "Output": 0})
    runner.step()
    runner.patch({"Enable": True})
    runner.step()
    assert runner.current_state.tags["Output"] == 2
