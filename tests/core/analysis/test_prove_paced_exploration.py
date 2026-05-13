"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Or,
    Program,
    Rung,
    Timer,
    latch,
    on_delay,
    out,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    prove,
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
# Paced exploration
# ===================================================================


class TestProvePaced:
    """prove(paced=True) separates paced from aggressive violations."""

    def test_paced_proves_aggressive_fails(self):
        """Oneshot chain requires back-to-back input flips without settling.
        Aggressive finds the violation; paced forces a stutter between flips
        so the oneshot pulse decays before the second input arrives."""
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        O1 = Bool("O1")
        O2 = Bool("O2")

        with Program(strict=False) as logic:
            with Rung(B, O1):
                out(O2, oneshot=True)
            with Rung(A, ~O2):
                out(O1, oneshot=True)

        unpaced = prove(logic, ~O2)
        assert isinstance(unpaced, Counterexample)

        result = prove(logic, ~O2, paced=True)
        assert isinstance(result, Proven)
        assert isinstance(result.aggressive_counterexample, Counterexample)

    def test_both_fail(self):
        """A property that fails under paced semantics returns Counterexample."""
        A = Bool("A", external=True)
        Alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(A):
                latch(Alarm)

        result = prove(logic, ~Alarm, paced=True)
        assert isinstance(result, Counterexample)

    def test_both_prove(self):
        """A universally true property has no aggressive_counterexample."""
        A = Bool("A", external=True)
        B = Bool("B")

        with Program(strict=False) as logic:
            with Rung(A):
                out(B)

        result = prove(logic, Or(~A, B), paced=True)
        assert isinstance(result, Proven)
        assert result.aggressive_counterexample is None

    def test_paced_and_settled(self):
        """paced and settled are orthogonal — both can be active."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("Fault")
        Alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Alarm)

        result = prove(logic, Or(~Cmd, Fb, Alarm), paced=True, settled=True)
        assert isinstance(result, Proven)

    def test_batch_paced(self):
        """Batch prove with paced: each property gets independent treatment."""
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        O1 = Bool("O1")
        X = Bool("X")
        Y = Bool("Y")

        with Program(strict=False) as logic:
            with Rung(B, O1):
                out(X, oneshot=True)
            with Rung(A, ~X):
                out(O1, oneshot=True)
            with Rung(A):
                latch(Y)

        results = prove(logic, [~X, ~Y], paced=True)
        assert isinstance(results, list)
        assert len(results) == 2

        assert isinstance(results[0], Proven)
        assert isinstance(results[0].aggressive_counterexample, Counterexample)

        assert isinstance(results[1], Counterexample)

    def test_aggressive_counterexample_replays(self):
        """The aggressive counterexample trace replays on the concrete PLC."""
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        O1 = Bool("O1")
        O2 = Bool("O2")

        with Program(strict=False) as logic:
            with Rung(B, O1):
                out(O2, oneshot=True)
            with Rung(A, ~O2):
                out(O1, oneshot=True)

        result = prove(logic, ~O2, paced=True)
        assert isinstance(result, Proven)
        assert result.aggressive_counterexample is not None

        plc = _replay_trace(logic, result.aggressive_counterexample.trace)
        assert plc.current_state.tags.get("O2") is True

    def test_paced_state_key_correctness(self):
        """Two independent inputs both reach True under paced exploration.
        Without the pacing bit in the state key, (T,T) would be unreachable."""
        A = Bool("A", external=True)
        B = Bool("B", external=True)
        Both = Bool("Both")

        with Program(strict=False) as logic:
            with Rung(A, B):
                latch(Both)

        result = prove(logic, ~Both, paced=True)
        assert isinstance(result, Counterexample)
