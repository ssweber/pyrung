"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Program,
    Rung,
    Timer,
    on_delay,
    out,
)
from pyrung.core.analysis.prove import (
    Intractable,
    TraceStep,
    prove,
    reachable_states,
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


# ===================================================================
# Group 5: Kernel oracle (soundness)
# ===================================================================


class TestKernelOracle:
    def test_timer_accumulation(self):
        """Timer Done bit flips after sufficient BFS depth."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=50)
            with Rung(t.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "T1_Done"], depth_budget=60)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("T1_Done") for s in states}
        assert True in done_values
        assert False in done_values
