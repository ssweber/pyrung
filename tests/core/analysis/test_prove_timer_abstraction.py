"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    Timer,
    calc,
    copy,
    count_up,
    off_delay,
    on_delay,
    out,
)
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    Proven,
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


class TestConsumedAccumulator:
    """Item 15: accumulator consumed in a condition stays as separate dimension."""

    def test_consumed_acc_kept_as_dimension(self):
        """Timer accumulator used as data flow is not threshold-abstracted."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        saved = Int("SavedAcc")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung():
                copy(t.Acc, saved)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, *_ = result
        assert "T1_Acc" in stateful
        assert "T1_Done" not in stateful


class TestTimerFastForward:
    """Item 18: timer with large preset exercises fast-forward budget."""

    def test_large_preset_reaches_done(self):
        """Timer with preset=500 (50K steps at dt=0.010) still reaches Done=True."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("BigT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=500)
            with Rung(t.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "BigT_Done"], depth_budget=10)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("BigT_Done") for s in states}
        assert True in done_values

    def test_done_preset_extracted(self):
        """_classify_dimensions extracts constant done presets by tag name."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=5000)
            with Rung(t.Done):
                out(Bool("Output"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        _s, _n, _c, _d, done_presets, _done_kinds = result
        assert done_presets["T1_Done"] == 5000

    def test_large_counter_reaches_done(self):
        """Count-up with a large preset still reaches Done=True via event jump."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        counter = Counter.clone("C1")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(counter, preset=500).reset(reset_btn)
            with Rung(counter.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "C1_Done"], depth_budget=10)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("C1_Done") for s in states}
        assert True in done_values

    def test_large_off_delay_reaches_expired(self):
        """Off-delay pending state eventually reaches Done=False via event jump."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("T1")
        expired = Bool("Expired")

        with Program(strict=False) as logic:
            with Rung(enable):
                off_delay(t, preset=500)
            with Rung(~t.Done):
                out(expired)

        states = reachable_states(logic, project=["Expired", "T1_Done"], depth_budget=10)
        assert not isinstance(states, Intractable)
        done_values = {dict(s).get("T1_Done") for s in states}
        assert False in done_values
        assert True in done_values


class TestDynamicPresetDoneEvent:
    """Tag-based timer/counter presets must produce BFS done-events."""

    def test_unconditional_tag_preset_timer_fires(self):
        """Timer with preset from an unconditional copy reaches Done=True."""
        enable = Bool("Enable", external=True)
        n0 = Int("N0")
        t = Timer.clone("T0")

        with Program(strict=False) as logic:
            with Rung():
                copy(50, n0)
            with Rung(enable):
                on_delay(t, n0)

        result = prove(logic, t.Done == False, depth_budget=20)  # noqa: E712
        assert isinstance(result, Counterexample)
        plc = _replay_trace(logic, result.trace)
        assert plc.current_state.tags["T0_Done"] is True

    def test_conditional_tag_preset_timer_fires(self):
        """Timer with preset from a conditional copy reaches Done=True."""
        enable = Bool("Enable", external=True)
        n0 = Int("N0")
        t = Timer.clone("T0")

        with Program(strict=False) as logic:
            with Rung(enable):
                copy(50, n0)
            with Rung(enable):
                on_delay(t, n0)

        result = prove(logic, t.Done == False, depth_budget=20)  # noqa: E712
        assert isinstance(result, Counterexample)
        plc = _replay_trace(logic, result.trace)
        assert plc.current_state.tags["T0_Done"] is True

    def test_tag_preset_reachable_states(self):
        """reachable_states includes Done=True for tag-preset timer."""
        enable = Bool("Enable", external=True)
        n0 = Int("N0")
        t = Timer.clone("T0")

        with Program(strict=False) as logic:
            with Rung():
                copy(50, n0)
            with Rung(enable):
                on_delay(t, n0)

        states = reachable_states(logic, project=["T0_Done"], depth_budget=20)
        assert not isinstance(states, Intractable)
        done_values = {dict(s)["T0_Done"] for s in states}
        assert done_values == {False, True}

    def test_tag_preset_counter_fires(self):
        """Count-up with tag-based preset reaches Done=True."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        n0 = Int("N0")
        c = Counter.clone("C0")

        with Program(strict=False) as logic:
            with Rung():
                copy(3, n0)
            with Rung(enable):
                count_up(c, n0).reset(reset_btn)

        result = prove(logic, c.Done == False, depth_budget=20)  # noqa: E712
        assert isinstance(result, Counterexample)


class TestRedundantTimerAccumulatorAbstraction:
    """Dynamic timer presets whose Acc comparisons collapse to Done state."""

    def test_final_preset_redundant_acc_comparison_is_absorbed(self):
        """A ladder-owned dynamic preset can be absorbed with its redundant Acc check."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        t = Timer.clone("DynT")
        output = Bool("Output")
        done_output = Bool("DoneOutput")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)
            with Rung(t.Done):
                out(done_output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, PENDING, True)
        assert "DynT_Acc" not in stateful
        assert "ActivePreset" not in stateful
        assert "ActivePreset" not in nd
        assert done_presets["DynT_Done"] == 1

        proved = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(proved, Proven)

    def test_literal_write_preset_redundant_acc_comparison_is_absorbed(self):
        """Literal-written presets absorb without readonly/final annotations."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset")
        t = Timer.clone("DynT")
        output = Bool("Output")
        done_output = Bool("DoneOutput")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)
            with Rung(t.Done):
                out(done_output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, PENDING, True)
        assert "DynT_Acc" not in stateful
        assert "ActivePreset" not in stateful
        assert "ActivePreset" not in nd
        assert done_presets["DynT_Done"] == 1

        proved = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(proved, Proven)

    def test_external_preset_redundant_acc_comparison_is_absorbed(self):
        """External presets still absorb when their value is threshold-only."""
        enable = Bool("Enable", external=True)
        hmi_preset = Int("HmiPreset", external=True, default=1)
        t = Timer.clone("DynT")
        output = Bool("Output")
        done_output = Bool("DoneOutput")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=hmi_preset)
            with Rung(t.Acc >= hmi_preset):
                out(output)
            with Rung(t.Done):
                out(done_output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, PENDING, True)
        assert "DynT_Acc" not in stateful
        assert "HmiPreset" not in stateful
        assert "HmiPreset" not in nd
        assert done_presets["DynT_Done"] == 1

        proved = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(proved, Proven)

    def test_zero_default_preset_blocks_absorption(self):
        """External preset with default=0 must not absorb — the comparison
        Acc >= 0 is trivially true at initialization."""
        enable = Bool("Enable", external=True)
        hmi_preset = Int("HmiPreset", external=True, min=0, max=3)
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=hmi_preset)
            with Rung(t.Acc >= hmi_preset):
                out(output)
            with Rung(t.Done):
                out(Bool("DoneOutput"))

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable), result
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DynT_Acc" in stateful, "Acc must not be absorbed with preset default=0"

        result = prove(logic, Or(~output, t.Done), depth_budget=5)
        assert isinstance(result, Counterexample)

    def test_non_redundant_acc_comparison_is_not_absorbed(self):
        """Strictly greater-than is absorbed by Layer 2 threshold events."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc > active_preset):
                out(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DynT_Acc" not in stateful
        assert "ActivePreset" not in stateful
        assert "ActivePreset" not in nd

        states = reachable_states(logic, project=["Output", "DynT_Done"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Output", True), ("DynT_Done", True)}) in states

    def test_preset_data_use_elsewhere_is_bounded_but_not_absorbed(self):
        """A preset copied elsewhere stays explicit instead of being absorbed."""
        enable = Bool("Enable", external=True)
        active_preset = Int("ActivePreset", final=True)
        copied_preset = Int("CopiedPreset")
        t = Timer.clone("DynT")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, active_preset)
                copy(active_preset, copied_preset)
            with Rung(enable):
                on_delay(t, preset=active_preset)
            with Rung(t.Acc >= active_preset):
                out(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, done_presets, _done_kinds = result
        assert "ActivePreset" in stateful
        assert "DynT_Acc" in stateful
        assert done_acc == {}
        assert done_presets == {}


class TestThresholdEventAbstraction:
    """Layer 2: progress threshold events for hidden accumulators."""

    def test_timer_threshold_event_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        active_threshold = Int("ActiveThreshold", final=True)
        t = Timer.clone("StepTmr")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["Alarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Alarm", True)}) in states

    def test_timer_threshold_event_allows_direct_zero_acc_reset(self):
        enable = Bool("ResettableTimerEnable", external=True)
        reset_btn = Bool("ResettableTimerReset", external=True)
        active_threshold = Int("ResettableTimerThreshold", final=True)
        t = Timer.clone("ResettableThresholdTmr")
        alarm = Bool("ResettableTimerAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(reset_btn):
                copy(0, t.Acc)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableTimerAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableTimerAlarm", True)}) in states

    def test_timer_acc_zero_copy_with_owner_is_valid_proof_input(self):
        """A data write to Timer.Acc is valid even when one timer owns the UDT."""
        enable = Bool("TimerAccCopyEnable", external=True)
        clear_acc = Bool("TimerAccCopyClear", external=True)
        t = Timer.clone("TimerAccCopyTmr")
        alarm = Bool("TimerAccCopyAlarm")

        with Program(strict=False) as logic:
            with Rung(clear_acc):
                copy(0, t.Acc)
            with Rung(enable):
                on_delay(t, preset=100)
            with Rung(t.Acc >= 10):
                out(alarm)

        optimized = prove(logic, ~alarm, max_states=10_000, depth_budget=20)
        unoptimized = prove(
            logic,
            ~alarm,
            max_states=10_000,
            depth_budget=20,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_timer_threshold_event_nonzero_acc_assignment_stays_explicit(self):
        enable = Bool("AssignedTimerEnable", external=True)
        force = Bool("AssignedTimerForce", external=True)
        active_threshold = Int("AssignedTimerThreshold", final=True)
        t = Timer.clone("AssignedThresholdTmr")
        alarm = Bool("AssignedTimerAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(force):
                copy(7, t.Acc)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "AssignedThresholdTmr_Acc" in stateful
        assert "AssignedTimerThreshold" in stateful
        assert done_acc == {}

    def test_multiple_timer_threshold_events_become_tractable(self):
        enable = Bool("Enable", external=True)
        pan = Int("PanThreshold", final=True)
        shaft = Int("ShaftThreshold", final=True)
        drip = Int("DripThreshold", final=True)
        t = Timer.clone("StepTmrMany")
        pan_alarm = Bool("PanAlarm")
        shaft_alarm = Bool("ShaftAlarm")
        drip_alarm = Bool("DripAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(200, pan)
                copy(400, shaft)
                copy(600, drip)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > pan):
                out(pan_alarm)
            with Rung(t.Acc > shaft):
                out(shaft_alarm)
            with Rung(t.Acc > drip):
                out(drip_alarm)

        states = reachable_states(
            logic,
            project=["PanAlarm", "ShaftAlarm", "DripAlarm"],
            depth_budget=10,
        )
        assert not isinstance(states, Intractable)
        assert (
            frozenset(
                {
                    ("PanAlarm", True),
                    ("ShaftAlarm", True),
                    ("DripAlarm", True),
                }
            )
            in states
        )

    def test_count_up_counter_threshold_event_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        active_threshold = Int("CounterThreshold", final=True)
        counter = Counter.clone("StepCounter")
        alarm = Bool("CounterAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(enable):
                count_up(counter, preset=1000).reset(reset_btn)
            with Rung(counter.Acc >= active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["CounterAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("CounterAlarm", True)}) in states

    def test_count_up_counter_threshold_event_allows_direct_zero_acc_reset(self):
        enable = Bool("ResettableCounterEnable", external=True)
        reset_btn = Bool("ResettableCounterReset", external=True)
        counter_reset = Bool("ResettableCounterOwnerReset", external=True)
        active_threshold = Int("ResettableCounterThreshold", final=True)
        counter = Counter.clone("ResettableThresholdCounter")
        alarm = Bool("ResettableCounterAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(reset_btn):
                copy(0, counter.Acc)
            with Rung(enable):
                count_up(counter, preset=1000).reset(counter_reset)
            with Rung(counter.Acc >= active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableCounterAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableCounterAlarm", True)}) in states

    def test_counter_acc_calc_with_owner_is_valid_proof_input(self):
        """A data write to Counter.Acc is valid even when one counter owns the UDT."""
        boost = Bool("CounterAccCalcBoost", external=True)
        enable = Bool("CounterAccCalcEnable", external=True)
        reset_btn = Bool("CounterAccCalcReset", external=True)
        counter = Counter.clone("CounterAccCalc")
        alarm = Bool("CounterAccCalcAlarm")

        with Program(strict=False) as logic:
            with Rung(boost):
                calc(counter.Acc + 5, counter.Acc)
            with Rung(enable):
                count_up(counter, preset=10).reset(reset_btn)
            with Rung(counter.Acc >= 5):
                out(alarm)

        optimized = prove(logic, ~alarm, max_states=10_000, depth_budget=20)
        unoptimized = prove(
            logic,
            ~alarm,
            max_states=10_000,
            depth_budget=20,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_internal_int_step_ticks_threshold_event_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("StepTicks")
        threshold = Int("TickThreshold", final=True)
        alarm = Bool("TickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["TickAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("TickAlarm", True)}) in states

    def test_constant_stride_tag_int_progress_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride")
        threshold = Int("VariableTickThreshold", final=True)
        alarm = Bool("VariableTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, stride)
                copy(500, threshold)
            with Rung(enable):
                calc(ticks + stride, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["VariableTickAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("VariableTickAlarm", True)}) in states

    def test_constant_stride_tag_int_progress_down_becomes_tractable(self):
        enable = Bool("Enable", external=True)
        ticks = Int("DescendingStepTicks", default=1000)
        stride = Int("DescendingStride")
        alarm = Bool("DescendingTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, stride)
            with Rung(enable):
                calc(ticks - stride, ticks)
            with Rung(ticks <= 500):
                out(alarm)

        states = reachable_states(logic, project=["DescendingTickAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("DescendingTickAlarm", True)}) in states

    def test_nonconstant_stride_tag_int_progress_stays_explicit(self):
        enable = Bool("Enable", external=True)
        sel = Bool("StrideSelect", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride")
        threshold = Int("VariableTickThreshold", final=True)
        alarm = Bool("VariableTickAlarm")

        with Program(strict=False) as logic:
            with Rung(sel):
                copy(1, stride)
            with Rung(~sel):
                copy(2, stride)
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                calc(ticks + stride, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "VariableStepTicks" in stateful
        assert "Stride" in stateful
        assert "VariableTickThreshold" in stateful

    def test_int_progress_eq_comparison_becomes_tractable(self):
        """calc(ticks + 1, ticks) with Rung(ticks == k) decomposes into ge/gt boundary atoms."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("EqTicks")
        at_five = Bool("AtFive")

        with Program(strict=False) as logic:
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks == 5):
                out(at_five)

        states = reachable_states(logic, project=["AtFive"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("AtFive", True)}) in states
        assert frozenset({("AtFive", False)}) in states

    def test_int_progress_ne_comparison_becomes_tractable(self):
        """calc(ticks + 1, ticks) with Rung(ticks != 0) decomposes into ge/gt boundary atoms."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("NeTicks")
        running = Bool("Running")

        with Program(strict=False) as logic:
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks != 0):
                out(running)

        states = reachable_states(logic, project=["Running"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Running", True)}) in states
        assert frozenset({("Running", False)}) in states

    def test_int_progress_eq_and_gt_mixed_becomes_tractable(self):
        """Progress tag with both == and > comparisons on the same accumulator."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        step = Int("MixedStep")
        threshold = Int("MixedThreshold", final=True)
        at_step = Bool("AtStep3")
        past_threshold = Bool("PastThreshold")

        with Program(strict=False) as logic:
            with Rung():
                copy(10, threshold)
            with Rung(reset_btn):
                copy(0, step)
            with Rung(enable):
                calc(step + 1, step)
            with Rung(step == 3):
                out(at_step)
            with Rung(step > threshold):
                out(past_threshold)

        states = reachable_states(logic, project=["AtStep3", "PastThreshold"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("AtStep3", True), ("PastThreshold", False)}) in states
        assert frozenset({("AtStep3", False), ("PastThreshold", True)}) in states
        assert frozenset({("AtStep3", True), ("PastThreshold", True)}) not in states

    def test_raw_external_threshold_is_absorbed_when_threshold_only(self):
        enable = Bool("Enable", external=True)
        hmi_threshold = Int("HmiThreshold", external=True, default=1000)
        t = Timer.clone("ExternalThresholdTmr")
        alarm = Bool("ExternalThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > hmi_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ExternalThresholdAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ExternalThresholdAlarm", False)}) in states
        assert frozenset({("ExternalThresholdAlarm", True)}) in states

    def test_unwritten_internal_threshold_is_absorbed_as_constant(self):
        enable = Bool("Enable", external=True)
        threshold = Int("ImplicitThreshold")
        t = Timer.clone("ImplicitThresholdTmr")
        alarm = Bool("ImplicitThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > threshold):
                out(alarm)

        states = reachable_states(logic, project=["ImplicitThresholdAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ImplicitThresholdAlarm", True)}) in states

    def test_projected_public_threshold_is_discovered(self):
        enable = Bool("Enable", external=True)
        active_threshold = Int("ProjectedThreshold", final=True, public=True)
        t = Timer.clone("ProjectedThresholdTmr")
        alarm = Bool("ProjectedThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, active_threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > active_threshold):
                out(alarm)

        states = reachable_states(
            logic,
            project=["ProjectedThresholdAlarm", "ProjectedThreshold"],
            depth_budget=5,
        )
        assert not isinstance(states, Intractable)
        threshold_vals = {dict(row)["ProjectedThreshold"] for row in states}
        assert 500 in threshold_vals

    def test_public_threshold_is_absorbed_when_not_projected(self):
        enable = Bool("Enable", external=True)
        public_threshold = Int("PublicThreshold", public=True, default=1000)
        t = Timer.clone("PublicThresholdTmr")
        alarm = Bool("PublicThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > public_threshold):
                out(alarm)

        states = reachable_states(logic, project=["PublicThresholdAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("PublicThresholdAlarm", False)}) in states
        assert frozenset({("PublicThresholdAlarm", True)}) in states

    def test_exact_and_abstract_threshold_events_both_branch(self):
        enable = Bool("Enable", external=True)
        hmi_threshold = Int("HmiThreshold", external=True, default=1000)
        t = Timer.clone("MixedThresholdTmr")
        exact_alarm = Bool("ExactAlarm")
        hmi_alarm = Bool("HmiAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > 500):
                out(exact_alarm)
            with Rung(t.Acc > hmi_threshold):
                out(hmi_alarm)

        states = reachable_states(logic, project=["ExactAlarm", "HmiAlarm"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("ExactAlarm", True), ("HmiAlarm", False)}) in states
        assert frozenset({("ExactAlarm", False), ("HmiAlarm", True)}) in states

    def test_non_threshold_accumulator_read_stays_explicit(self):
        enable = Bool("Enable", external=True)
        threshold = Int("SavedAccThreshold", final=True)
        t = Timer.clone("SavedAccTmr")
        saved = Int("SavedAccValue")
        alarm = Bool("SavedAccAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung():
                copy(t.Acc, saved)
            with Rung(t.Acc > threshold):
                out(alarm)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, done_acc, _done_presets, _done_kinds = result
        assert "SavedAccTmr_Acc" in stateful
        assert "SavedAccThreshold" in stateful
        assert done_acc == {}

    def test_reset_recomputes_threshold_vector_false(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("ResetTicks", external=True)
        ticks = Int("ResettableStepTicks")
        threshold = Int("ResettableTickThreshold", final=True)
        alarm = Bool("ResettableTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(3, threshold)
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableTickAlarm"], depth_budget=8)
        assert not isinstance(states, Intractable)
        assert frozenset({("ResettableTickAlarm", True)}) in states
        assert frozenset({("ResettableTickAlarm", False)}) in states

    def test_threshold_event_before_done_uses_nearest_event(self):
        enable = Bool("Enable", external=True)
        threshold = Int("NearestThreshold", final=True)
        t = Timer.clone("NearestTmr")
        warning = Bool("Warning")

        with Program(strict=False) as logic:
            with Rung():
                copy(500, threshold)
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > threshold):
                out(warning)

        states = reachable_states(logic, project=["Warning", "NearestTmr_Done"], depth_budget=5)
        assert not isinstance(states, Intractable)
        assert frozenset({("Warning", True), ("NearestTmr_Done", False)}) in states


def test_co_pending_timers_do_not_inflate_visited_set():
    """Two timers PENDING simultaneously should not bloat the BFS visited set.

    When both T1 and T2 are PENDING, the hidden-event phase key embeds
    per-tick accumulator snapshots into the state key, creating a unique
    visited entry every scan.  With enough nondeterministic inputs the
    inflated set exceeds max_states even though the abstract state space
    is only ~11 states.
    """
    In0 = Bool("In0", external=True)
    In1 = Bool("In1", external=True)
    In2 = Bool("In2", external=True)
    In3 = Bool("In3", external=True)
    B0 = Bool("B0")
    B1 = Bool("B1")
    B2 = Bool("B2")
    T1 = Timer.clone("T1")
    T2 = Timer.clone("T2")

    with Program(strict=False) as logic:
        with Rung(In0):
            on_delay(T1, 500)
        with Rung(In0):
            on_delay(T2, 700)
        with Rung(T1.Done, In1):
            out(B0)
        with Rung(T2.Done, In2):
            out(B1)
        with Rung(T1.Done, T2.Done, In3):
            out(B2)

    states = reachable_states(
        logic,
        project=["B0", "B1", "B2", "T1_Done", "T2_Done"],
        max_states=50,
        depth_budget=50,
    )
    assert not isinstance(states, Intractable), (
        f"visited set inflated to {states.estimated_space}; "
        f"abstract state space is ~11"
    )
    assert len(states) == 11
