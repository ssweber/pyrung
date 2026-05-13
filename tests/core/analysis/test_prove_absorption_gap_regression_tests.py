"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    copy,
    count_down,
    count_up,
    latch,
    on_delay,
    out,
)
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    TraceStep,
    _classify_dimensions,
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
# Absorption gap regression tests
# ===================================================================


class TestThresholdProgressAbsorptionGaps:
    def test_threshold_only_progress_keeps_immediate_counterexample(self):
        """Threshold jumps must not hide the concrete post-scan state."""
        in0 = Bool("In0", external=True)
        d0 = Dint("D0")

        with Program(strict=False) as logic:
            with Rung(in0):
                calc(d0 + 1, d0)
            with Rung(in0):
                copy(d0, d0)
            with Rung(in0):
                copy(d0, d0)

        result = prove(logic, d0 < 1, max_states=10_000, depth_budget=20)

        assert isinstance(result, Counterexample)
        assert any(step.inputs.get("In0") is True for step in result.trace)


class TestCountDownAbsorptionGaps:
    """CountDown counters are excluded from all absorption paths.

    Verify that prove() still reaches Done=True states and that
    intermediate-Acc-dependent outputs are reachable.
    """

    def test_count_down_done_is_reachable(self):
        """Basic count_down: prove() should find the Done=True state."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDReset", external=True)
        counter = Counter.clone("CTD")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(rst)
            with Rung(counter.Done):
                out(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample), "count_down Done=True should be reachable"

    def test_count_down_consumed_acc_done_still_reachable(self):
        """count_down with Acc comparison — Done must still be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDAccReset", external=True)
        counter = Counter.clone("CTDAcc")
        warning = Bool("Warning")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(rst)
            with Rung(counter.Acc <= -3):
                out(warning)
            with Rung(counter.Done):
                out(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample), (
            "count_down with consumed Acc: Done=True should be reachable"
        )

    def test_count_down_reset_via_threshold_comparison_allows_absorption(self):
        """Reset fed by a threshold comparison is threshold-mediated — absorption is safe."""
        enable = Bool("Enable", external=True)
        reset_from_threshold = Bool("ResetFromThreshold")
        counter = Counter.clone("ResetFedCtd")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(reset_from_threshold)
            with Rung(counter.Acc <= -3):
                out(reset_from_threshold)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "ResetFedCtd_Acc" not in stateful

    def test_count_down_reset_via_data_copy_of_acc_blocks_absorption(self):
        """Reset fed by a data copy of the accumulator is NOT threshold-mediated."""
        enable = Bool("Enable", external=True)
        acc_mirror = Int("AccMirror")
        reset_helper = Bool("ResetHelper")
        counter = Counter.clone("DataFedCtd")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(reset_helper)
            with Rung():
                copy(counter.Acc, acc_mirror)
            with Rung(acc_mirror <= -3):
                out(reset_helper)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DataFedCtd_Acc" in stateful

    def test_count_down_intermediate_state_reachable(self):
        """count_down intermediate output (Acc-dependent) must be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDMidReset", external=True)
        counter = Counter.clone("CTDMid")
        midway = Bool("Midway")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=10).reset(rst)
            with Rung(counter.Acc < -3):
                out(midway)

        states = reachable_states(logic, project=["Midway"])
        assert not isinstance(states, Intractable), (
            "count_down with Acc comparison should be tractable"
        )
        assert frozenset({("Midway", True)}) in states, "Midway should be reachable when Acc < -3"


class TestBidirectionalCounterGaps:
    """CountUp with down_condition is excluded from progress source detection."""

    def test_bidirectional_counter_done_is_reachable(self):
        """count_up with .down() — Done should still be reachable."""
        up_btn = Bool("UpBtn", external=True)
        down_btn = Bool("DownBtn", external=True)
        rst = Bool("BiDirReset", external=True)
        counter = Counter.clone("BiDir")
        alarm = Bool("BiDirAlarm")

        with Program(strict=False) as logic:
            with Rung(up_btn):
                count_up(counter, preset=5).down(down_btn).reset(rst)
            with Rung(counter.Done):
                out(alarm)

        result = prove(logic, ~alarm)
        assert isinstance(result, Counterexample), (
            "bidirectional counter Done=True should be reachable"
        )

    def test_bidirectional_counter_threshold_reachable(self):
        """count_up with .down() and threshold comparison."""
        up_btn = Bool("UpBtn", external=True)
        down_btn = Bool("DownBtn", external=True)
        rst = Bool("BiDirThrReset", external=True)
        counter = Counter.clone("BiDirThr")
        threshold = Int("BiDirThreshold", final=True)
        halfway = Bool("BiDirHalfway")

        with Program(strict=False) as logic:
            with Rung():
                copy(3, threshold)
            with Rung(up_btn):
                count_up(counter, preset=5).down(down_btn).reset(rst)
            with Rung(counter.Acc >= threshold):
                out(halfway)

        states = reachable_states(logic, project=["BiDirHalfway"])
        assert not isinstance(states, Intractable), (
            "bidirectional counter with threshold should be tractable"
        )
        assert frozenset({("BiDirHalfway", True)}) in states


class TestTruthyAccAbsorptionGaps:
    """Timer/Counter Acc used as a boolean condition (truthy/xic form).

    _threshold_atom_for_progress and _atom_matches_acc_preset_boundary
    don't handle truthy atoms, blocking both absorption paths.  The Acc
    gets an empty domain and drops out of the state key entirely.
    """

    def test_timer_acc_truthy_done_still_reachable(self):
        """Timer Acc as truthy condition — Done must still be reachable."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("TruthyTmr")
        timing = Bool("Timing")
        complete = Bool("Complete")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc):
                out(timing)
            with Rung(t.Done):
                out(complete)

        result = prove(logic, ~complete)
        assert isinstance(result, Counterexample), (
            "timer with truthy Acc: Done=True should be reachable"
        )

    def test_counter_acc_nonzero_done_still_reachable(self):
        """Counter Acc > 0 condition — Done must still be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("TruthyCtrReset", external=True)
        c = Counter.clone("TruthyCtr")
        counting = Bool("Counting")
        complete = Bool("CountComplete")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=10).reset(rst)
            with Rung(c.Acc > 0):
                out(counting)
            with Rung(c.Done):
                out(complete)

        result = prove(logic, ~complete)
        assert isinstance(result, Counterexample), (
            "counter with Acc > 0: Done=True should be reachable"
        )

    def test_timer_acc_truthy_timing_state_reachable(self):
        """Truthy Acc guard: the 'timing active' output must be reachable."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("TruthyTmr2")
        active = Bool("TimerActive")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc):
                latch(active)

        result = prove(logic, ~active)
        assert isinstance(result, Counterexample), (
            "timer Acc truthy: 'active' should be reachable via latched output"
        )


class TestCounterResetReachability:
    """Verify that reset-during-counting states are reachable."""

    def test_count_up_reset_during_count_is_reachable(self):
        """Reset mid-count: prove should find the state where reset fires while counting."""
        enable = Bool("Enable", external=True)
        rst = Bool("ResetBtn", external=True)
        c = Counter.clone("RstCtr")
        counting = Bool("Counting")
        was_reset = Bool("WasReset")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=10).reset(rst)
            with Rung(c.Acc > 0):
                latch(counting)
            with Rung(counting, c.Acc == 0):
                latch(was_reset)

        result = prove(logic, ~was_reset)
        assert isinstance(result, Counterexample), (
            "reset-during-count should be reachable: counting=True then Acc=0 after reset"
        )


class TestConstantPresetCounterAbsorption:
    """Verify the e6a0d4f fix also applies to count_up counters."""

    def test_count_up_constant_preset_acc_lt_absorbed(self):
        """count_up with constant preset and Acc < Preset should absorb."""
        enable = Bool("Enable", external=True)
        rst = Bool("ConstCtrReset", external=True)
        c = Counter.clone("ConstCtr")
        almost = Bool("AlmostDone")
        done_out = Bool("DoneOut")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=1000).reset(rst)
            with Rung(c.Acc < 1000):
                out(almost)
            with Rung(c.Done):
                out(done_out)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert stateful.get("ConstCtr_Done") == (False, PENDING, True), (
            "count_up with constant preset and Acc < Preset: Done should get 3-valued domain"
        )
        assert "ConstCtr_Acc" not in stateful, (
            "count_up Acc should be absorbed (redundant with Done boundary)"
        )

    def test_count_up_constant_preset_acc_le_not_same_boundary(self):
        """Acc <= Preset is NOT the same boundary as Acc < Preset; threshold absorption handles it."""
        enable = Bool("Enable", external=True)
        rst = Bool("LeCtrReset", external=True)
        c = Counter.clone("LeCtr")
        output = Bool("LeOutput")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=1000).reset(rst)
            with Rung(c.Acc <= 1000):
                out(output)

        states = reachable_states(logic, project=["LeOutput", "LeCtr_Done"])
        assert not isinstance(states, Intractable)
        assert any(("LeCtr_Done", True) in s for s in states), (
            "Done=True should be reachable even with Acc <= Preset comparison"
        )
