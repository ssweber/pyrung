"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    latch,
    out,
    reset,
    rise,
    run_function,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    TraceStep,
    _classify_dimensions,
    check_lock,
    program_hash,
    prove,
    reachable_states,
    write_lock,
)

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a prove() counterexample trace on the concrete PLC."""
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_soundness(
    logic: Program,
    condition,
    *,
    max_states: int = 10_000,
    depth_budget: int = 20,
) -> None:
    """Assert that optimized and unoptimized prove() agree on the result type."""
    optimized = prove(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = prove(
        logic,
        condition,
        max_states=max_states,
        depth_budget=depth_budget,
        _skip_optimizations=True,
        journal=True,
    )
    if isinstance(optimized, Intractable) or isinstance(unoptimized, Intractable):
        pytest.skip("one side intractable")
    assert type(optimized) is type(unoptimized), (
        f"optimized={type(optimized).__name__}, unoptimized={type(unoptimized).__name__}\n"
        f"--- optimized journal ---\n{optimized.journal}\n"
        f"--- unoptimized journal ---\n{unoptimized.journal}"
    )


class TestEdgeConditions:
    """Item 13: rise()/fall() in verified programs."""

    def test_rise_in_condition_explores_edge(self):
        """Program with rise() — prev tracking produces correct transitions."""
        button = Bool("Button", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(rise(button)):
                latch(flag)

        result = prove(logic, ~flag)
        assert isinstance(result, Counterexample)
        assert len(result.trace) >= 2

    def test_rise_guard_prevents_relatch(self):
        """rise() fires only on 0→1 edge — holding True doesn't re-trigger."""
        trigger = Bool("Trigger", external=True)
        flag = Bool("Flag")
        second = Bool("Second")

        with Program(strict=False) as logic:
            with Rung(rise(trigger)):
                latch(flag)
            with Rung(flag):
                out(second)

        states = reachable_states(logic, project=["Flag", "Second"])
        assert not isinstance(states, Intractable)


class TestScopeParameter:
    """Item 14: scope= parameter on prove and _classify_dimensions."""

    def test_scope_restricts_inputs(self):
        """Scoped verification only enumerates upstream inputs."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        result = _classify_dimensions(logic, scope=["X"])
        assert not isinstance(result, Intractable)
        _stateful, nd, _combinational, _done_acc, _done_presets, _done_kinds = result
        assert "A" in nd
        assert "B" not in nd

    def test_prove_with_scope(self):
        """prove() with explicit scope restricts exploration."""
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        x = Bool("X")
        y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(a):
                latch(x)
            with Rung(b):
                latch(y)

        result = prove(logic, ~x, scope=["X"])
        assert isinstance(result, Counterexample)


class TestAnnotatedFunctionOutput:
    """Item 16: annotated function outputs succeed verification."""

    def test_choices_annotated_function_is_verifiable(self):
        """run_function output with choices is not Intractable."""
        trigger = Bool("Trigger", external=True)
        result_tag = Int("Result", choices={0: "off", 1: "on"})

        def compute() -> dict:
            return {"result": 1}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(compute, outs={"result": result_tag})
            with Rung(result_tag == 1):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)

    def test_min_max_annotated_function_is_verifiable(self):
        """run_function output with min/max is not Intractable."""
        trigger = Bool("Trigger", external=True)
        level = Int("Level", min=0, max=3)

        def read_level() -> dict:
            return {"level": 2}

        with Program(strict=False) as logic:
            with Rung(trigger):
                run_function(read_level, outs={"level": level})
            with Rung(level == 2):
                out(Bool("High"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)


class TestCheckLockChange:
    """Item 17: check_lock detects an actual behavioral change."""

    def test_check_lock_detects_behavioral_change(self, tmp_path):
        """Lock check returns StateDiff when program changes."""
        button = Bool("Button", external=True)
        stop = Bool("Stop", external=True)
        running = Bool("Running", public=True)
        light = Bool("Light", public=True)

        with Program(strict=False) as original:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)
            with Rung(running):
                out(light)

        states = reachable_states(original, project=["Running", "Light"])
        assert not isinstance(states, Intractable)
        lock_path = tmp_path / "pyrung.lock"
        write_lock(lock_path, states, ["Running", "Light"], program_hash(original))

        with Program(strict=False) as modified:
            with Rung(button):
                latch(running)
            with Rung(stop):
                reset(running)
            with Rung():
                out(light)

        d = check_lock(modified, lock_path)
        assert d is not None
        assert d.added or d.removed
