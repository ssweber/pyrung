"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    latch,
    on_delay,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    prove,
)

from .conftest import no_agreement

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


class TestPendingSettlementChains:
    """Pending settlement should fully resolve chained hidden-event work."""

    def test_prove_settles_chained_exact_timers_before_reporting_failure(self):
        """A false pending plateau should settle through both exact timers first."""
        cmd = Bool("Cmd", external=True)
        fb = Bool("Fb", external=True)
        t1 = Timer.clone("ChainT1")
        t2 = Timer.clone("ChainT2")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(cmd, ~fb):
                on_delay(t1, preset=30)
            with Rung(t1.Done):
                on_delay(t2, preset=30)
            with Rung(t2.Done):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Cmd": True, "Fb": False})
        for _ in range(5):
            plc.step()
        assert plc.current_state.tags.get("Alarm") is True

        unsettled = prove(logic, Or(~cmd, fb, alarm), depth_budget=5)
        assert isinstance(unsettled, Counterexample)

        result = prove(logic, Or(~cmd, fb, alarm), depth_budget=5, settled=True)
        assert isinstance(result, Proven)

    @no_agreement
    def test_prove_settles_exact_timer_started_by_abstract_threshold_branch(self):
        """Abstract threshold branches should keep settling exact work they enable."""
        enable = Bool("Enable", external=True)
        hidden_threshold = Int(
            "HiddenThreshold",
            external=True,
            choices={10: "Trip"},
            default=10,
        )
        t1 = Timer.clone("AbstractChainT1")
        t2 = Timer.clone("AbstractChainT2")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t1, preset=30)
            with Rung(t1.Acc > hidden_threshold):
                on_delay(t2, preset=30)
            with Rung(t2.Done):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True})
        for _ in range(4):
            plc.step()
        assert plc.current_state.tags.get("Alarm") is True

        unsettled = prove(logic, Or(~enable, alarm), depth_budget=5)
        assert isinstance(unsettled, Counterexample)

        result = prove(logic, Or(~enable, alarm), depth_budget=5, settled=True)
        assert isinstance(result, Proven)


class TestSettlePending:
    """prove() settles pending timers before reporting counterexamples."""

    def test_timer_gated_alarm_proves_with_settle(self):
        """A property guarded by a timer-gated alarm should prove, not produce
        a spurious counterexample from the PENDING state."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("Fault")
        Alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Alarm)

        unsettled = prove(logic, Or(~Cmd, Fb, Alarm))
        assert isinstance(unsettled, Counterexample)

        result = prove(logic, Or(~Cmd, Fb, Alarm), settled=True)
        assert isinstance(result, Proven)

    def test_genuinely_missing_alarm_still_counterexample(self):
        """A feedback fault with no alarm should produce a Counterexample.
        Uses the same timer pattern but proves a property that is NOT
        reachable — Running latches but the property demands ~Running."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("NoAlarm")
        Running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Running)

        result = prove(logic, ~Running)
        assert isinstance(result, Counterexample)

    def test_batch_prove_settles_pending(self):
        """Batch mode also settles pending timers."""
        Cmd = Bool("Cmd", external=True)
        Fb = Bool("Fb", external=True)
        FaultDone = Timer.clone("Fault")
        Alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(Cmd, ~Fb):
                on_delay(FaultDone, 3000)
            with Rung(FaultDone.Done):
                latch(Alarm)

        unsettled = prove(logic, [Or(~Cmd, Fb, Alarm)])
        assert isinstance(unsettled, list)
        assert isinstance(unsettled[0], Counterexample)

        results = prove(logic, [Or(~Cmd, Fb, Alarm)], settled=True)
        assert isinstance(results, list)
        assert isinstance(results[0], Proven)
