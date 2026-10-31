"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Counter,
    Int,
    Program,
    Rung,
    Timer,
    calc,
    copy,
    count_up,
    latch,
    off_delay,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    _classify_dimensions,
    always,
    never,
    reachable_states,
)

prove_module = importlib.import_module("pyrung.core.analysis.prove")


def _replay_trace(program: Program, trace: list[TraceStep]) -> PLC:
    """Replay a always() counterexample trace on the concrete PLC."""
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
    """Assert that optimized and unoptimized always() agree on the result type."""
    optimized = always(
        logic, condition, max_states=max_states, depth_budget=depth_budget, journal=True
    )
    unoptimized = always(
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
        """Timer accumulator used as data flow is state; its Done is a plain Bool."""
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
        assert stateful["T1_Done"] == (False, True)


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

    def test_counter_reaches_done_edge_by_edge(self):
        """A counter counts concretely, one rising edge every two scans: a preset
        within the depth budget is reached (no hidden-event jump covers a large one)."""
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        counter = Counter.clone("C1")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(counter, preset=5).reset(reset_btn)
            with Rung(counter.Done):
                out(output)

        states = reachable_states(logic, project=["Output", "C1_Done"], depth_budget=20)
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


def _first_hit_scan(logic: Program, tag_name: str, inputs: dict, limit: int) -> int | None:
    plc = PLC(logic, dt=0.010)
    plc.patch(inputs)
    for scan in range(1, limit + 1):
        plc.step()
        if plc.current_state.tags[tag_name]:
            return scan
    return None


def _assert_hit_replays(logic: Program, result, tag_name: str, scans: int | None = None) -> None:
    assert isinstance(result, Counterexample)
    assert not result.caveats
    assert _replay_trace(logic, result.trace).current_state.tags[tag_name] is True
    if scans is not None:
        assert sum(step.scans for step in result.trace) == scans


_COARSE_UNITS = [("sec", 1), ("Ts", 2), ("min", 1), ("ms", 1500)]


class TestTimerUnitsCoarserThanTheScan:
    """A timer in seconds or minutes runs for many scans on its fractional
    carry alone (``Acc`` stays 0 at 10 ms): that is still progress, and the
    hidden-event jump lands where the per-scan carry recurrence does."""

    @pytest.mark.parametrize(("unit", "preset"), _COARSE_UNITS)
    def test_on_delay_reaches_done(self, unit, preset):
        tag = f"CoarseTon{unit}"
        enable = Bool(tag + "En", external=True)
        timer = Timer.clone(tag)
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(timer, preset=preset, unit=unit)
            with Rung(timer.Done):
                out(hit)
        truth = _first_hit_scan(logic, hit.name, {enable.name: True}, 20_000)
        _assert_hit_replays(logic, always(logic, ~hit), hit.name, truth)

    @pytest.mark.parametrize(("unit", "preset"), _COARSE_UNITS)
    def test_retentive_and_off_delay_reach_done(self, unit, preset):
        tag = f"CoarseRet{unit}"
        enable = Bool(tag + "En", external=True)
        clear = Bool(tag + "Clr", external=True)
        retentive = Timer.clone(tag + "R")
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(retentive, preset=preset, unit=unit).reset(clear)
            with Rung(retentive.Done):
                out(hit)
        _assert_hit_replays(logic, always(logic, ~hit), hit.name)

        tag = f"CoarseTof{unit}"
        enable = Bool(tag + "En", external=True)
        seen = Bool(tag + "Seen")
        off = Timer.clone(tag)
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(enable):
                latch(seen)
            with Rung(enable):
                off_delay(off, preset=preset, unit=unit)
            with Rung(seen, ~enable, ~off.Done):
                out(hit)
        _assert_hit_replays(logic, always(logic, ~hit), hit.name)

    def test_off_delay_at_preset_zero_keeps_timing(self):
        """Preset 0: the first off scan leaves Done False, Acc 0 (the resting
        pair) on its carry alone, and the timer keeps timing from there.  Only
        its ``_tof_timing`` marker tells that state from the initial one, so
        the state key must carry it or the two merge and Acc >= 1 is missed."""
        enable = Bool("CoarseTof0En", external=True)
        timer = Timer.clone("CoarseTof0")
        hit = Bool("CoarseTof0Hit")
        with Program(strict=False) as logic:
            with Rung(enable):
                off_delay(timer, preset=0, unit="Ts")
            with Rung(timer.Acc >= 1):
                out(hit)
        _assert_hit_replays(logic, always(logic, ~hit), hit.name)

    @pytest.mark.parametrize(("unit", "preset"), _COARSE_UNITS)
    def test_accumulator_threshold_is_crossed(self, unit, preset):
        tag = f"CoarseAcc{unit}"
        enable = Bool(tag + "En", external=True)
        timer = Timer.clone(tag)
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(timer, preset=preset * 4, unit=unit)
            with Rung(timer.Acc > preset):
                out(hit)
        truth = _first_hit_scan(logic, hit.name, {enable.name: True}, 40_000)
        _assert_hit_replays(logic, always(logic, ~hit), hit.name, truth)

    @pytest.mark.parametrize(("unit", "preset"), [("min", 1), ("Ts", 2)])
    def test_retentive_timer_on_a_toggling_bit_is_pumped_exactly(self, unit, preset):
        """Pumped around the two-scan cycle on the carry recurrence: an
        aggregate float jump lands a couple of scans short of the preset."""
        tag = f"CoarseOsc{unit}"
        toggle = Bool(tag + "Tog")
        clear = Bool(tag + "Clr", external=True)
        timer = Timer.clone(tag)
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(~toggle):
                out(toggle)
            with Rung(toggle):
                on_delay(timer, preset=preset, unit=unit).reset(clear)
            with Rung(timer.Done):
                out(hit)
        truth = _first_hit_scan(logic, hit.name, {clear.name: False}, 40_000)
        _assert_hit_replays(logic, always(logic, ~hit), hit.name, truth)

    def test_timer_on_a_toggling_bit_is_unconfirmed(self):
        """Enabled every other scan, the timer restarts before 1 s on the PLC.
        That is timing: the search lets it finish on an enabled scan, replay
        does not reach the hit, and the result is unconfirmed, not Proven."""
        toggle = Bool("CoarseNeverTog")
        timer = Timer.clone("CoarseNever")
        hit = Bool("CoarseNeverHit")
        with Program(strict=False) as logic:
            with Rung(~toggle):
                out(toggle)
            with Rung(toggle):
                on_delay(timer, preset=1, unit="sec")
            with Rung(timer.Done):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Intractable)
        assert result.kind == "timing"
        assert "CoarseNever_Done" in result.tags
        assert result.trace


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

        result = always(logic, t.Done == False, depth_budget=20)  # noqa: E712
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

        result = always(logic, t.Done == False, depth_budget=20)  # noqa: E712
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

        result = always(logic, c.Done == False, depth_budget=20)  # noqa: E712
        assert isinstance(result, Counterexample)


class TestTimerAccumulatorAgainstItsPreset:
    """A timer whose Acc is compared with its own dynamic preset: the search
    tracks the accumulator (black-box timers absorb nothing) and the tick that
    reaches the preset sets Done on the same scan."""

    def test_final_preset_acc_comparison_follows_done(self):
        """A ladder-owned dynamic preset: Acc >= preset never holds without Done."""
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
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, True)
        assert "DynT_Acc" in stateful

        proved = never(logic, output, ~t.Done, depth_budget=5)
        assert isinstance(proved, Proven)

    def test_literal_write_preset_acc_comparison_follows_done(self):
        """Literal-written presets need no readonly/final annotation."""
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
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert stateful["DynT_Done"] == (False, True)
        assert "DynT_Acc" in stateful

        proved = never(logic, output, ~t.Done, depth_budget=5)
        assert isinstance(proved, Proven)

    def test_external_preset_needs_a_domain(self):
        """An HMI preset compared with the accumulator is a value the search
        must enumerate: without min/max it is Intractable naming it; bounded,
        Acc >= preset still never holds without Done."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("DynT")
        output = Bool("Output")

        def program(hmi_preset: Int) -> Program:
            with Program(strict=False) as logic:
                with Rung(enable):
                    on_delay(t, preset=hmi_preset)
                with Rung(t.Acc >= hmi_preset):
                    out(output)
                with Rung(t.Done):
                    out(Bool("DoneOutput"))
            return logic

        unbounded = never(
            program(Int("HmiPreset", external=True, default=1)), output, ~t.Done, depth_budget=5
        )
        assert isinstance(unbounded, Intractable)
        assert unbounded.kind == "search"
        assert "HmiPreset" in unbounded.tags

        bounded = program(Int("HmiPreset", external=True, default=1, min=1, max=5))
        assert isinstance(never(bounded, output, ~t.Done, depth_budget=5), Proven)

    def test_zero_default_preset_reaches_output_without_done(self):
        """External preset with default=0: Acc >= 0 holds at power-on, before
        the timer's Done."""
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
        assert "DynT_Acc" in stateful

        result = never(logic, output, ~t.Done, depth_budget=5)
        assert isinstance(result, Counterexample)

    def test_acc_past_the_preset_is_reached(self):
        """Strictly greater than the preset: the tick may land past it."""
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
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DynT_Acc" in stateful

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


class TestAccumulatorThresholds:
    """An accumulator compared against thresholds.  A timer's tick may land on
    any value a comparison splits at, so a threshold far past the budget's
    scans is still crossed; a counter or a calc accumulator steps concretely,
    so its thresholds must lie within depth_budget."""

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

        optimized = always(logic, ~alarm, max_states=10_000, depth_budget=20)
        unoptimized = always(
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

    def test_count_up_counter_threshold_is_counted_to(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        active_threshold = Int("CounterThreshold", final=True)
        counter = Counter.clone("StepCounter")
        alarm = Bool("CounterAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(5, active_threshold)
            with Rung(enable):
                count_up(counter, preset=10).reset(reset_btn)
            with Rung(counter.Acc >= active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["CounterAlarm"], depth_budget=20)
        assert not isinstance(states, Intractable)
        assert frozenset({("CounterAlarm", True)}) in states

    def test_count_up_counter_threshold_allows_direct_zero_acc_reset(self):
        enable = Bool("ResettableCounterEnable", external=True)
        reset_btn = Bool("ResettableCounterReset", external=True)
        counter_reset = Bool("ResettableCounterOwnerReset", external=True)
        active_threshold = Int("ResettableCounterThreshold", final=True)
        counter = Counter.clone("ResettableThresholdCounter")
        alarm = Bool("ResettableCounterAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(5, active_threshold)
            with Rung(reset_btn):
                copy(0, counter.Acc)
            with Rung(enable):
                count_up(counter, preset=10).reset(counter_reset)
            with Rung(counter.Acc >= active_threshold):
                out(alarm)

        states = reachable_states(logic, project=["ResettableCounterAlarm"], depth_budget=20)
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

        optimized = always(logic, ~alarm, max_states=10_000, depth_budget=20)
        unoptimized = always(
            logic,
            ~alarm,
            max_states=10_000,
            depth_budget=20,
            _skip_optimizations=True,
        )

        assert isinstance(optimized, Counterexample)
        assert type(optimized) is type(unoptimized)

    def test_internal_int_step_ticks_threshold_is_counted_to(self):
        enable = Bool("Enable", external=True)
        reset_btn = Bool("Reset", external=True)
        ticks = Int("StepTicks")
        threshold = Int("TickThreshold", final=True)
        alarm = Bool("TickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(5, threshold)
            with Rung(reset_btn):
                copy(0, ticks)
            with Rung(enable):
                calc(ticks + 1, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["TickAlarm"], depth_budget=10)
        assert not isinstance(states, Intractable)
        assert frozenset({("TickAlarm", True)}) in states

    def test_constant_stride_tag_int_progress_is_counted_to(self):
        enable = Bool("Enable", external=True)
        ticks = Int("VariableStepTicks")
        stride = Int("Stride")
        threshold = Int("VariableTickThreshold", final=True)
        alarm = Bool("VariableTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, stride)
                copy(5, threshold)
            with Rung(enable):
                calc(ticks + stride, ticks)
            with Rung(ticks > threshold):
                out(alarm)

        states = reachable_states(logic, project=["VariableTickAlarm"], depth_budget=10)
        assert not isinstance(states, Intractable)
        assert frozenset({("VariableTickAlarm", True)}) in states

    def test_constant_stride_tag_int_progress_down_is_counted_to(self):
        enable = Bool("Enable", external=True)
        ticks = Int("DescendingStepTicks", default=10)
        stride = Int("DescendingStride")
        alarm = Bool("DescendingTickAlarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(1, stride)
            with Rung(enable):
                calc(ticks - stride, ticks)
            with Rung(ticks <= 5):
                out(alarm)

        states = reachable_states(logic, project=["DescendingTickAlarm"], depth_budget=10)
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

    def test_int_progress_eq_and_gt_mixed(self):
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

        states = reachable_states(logic, project=["AtStep3", "PastThreshold"], depth_budget=15)
        assert not isinstance(states, Intractable)
        assert frozenset({("AtStep3", True), ("PastThreshold", False)}) in states
        assert frozenset({("AtStep3", False), ("PastThreshold", True)}) in states
        assert frozenset({("AtStep3", True), ("PastThreshold", True)}) not in states

    @pytest.mark.parametrize(
        "threshold",
        [
            Int("HmiThreshold", external=True, default=1000),
            Int("ImplicitThreshold"),
            Int("PublicThreshold", public=True, default=1000),
        ],
        ids=["external", "unwritten", "public"],
    )
    def test_threshold_tag_without_a_domain_is_intractable_naming_it(self, threshold):
        """A threshold tag nothing in the program bounds (an HMI value, an
        unwritten or public tag) is a value the search must enumerate: it is
        no longer absorbed, so it needs choices= or min=/max=."""
        enable = Bool("Enable", external=True)
        t = Timer.clone("ThresholdTmr")
        alarm = Bool("ThresholdAlarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(t, preset=1000)
            with Rung(t.Acc > threshold):
                out(alarm)

        states = reachable_states(logic, project=["ThresholdAlarm"], depth_budget=5)
        assert isinstance(states, Intractable)
        assert states.kind == "search"
        assert threshold.name in states.tags

    def test_bounded_external_threshold_is_crossed(self):
        enable = Bool("Enable", external=True)
        hmi_threshold = Int(
            "HmiThreshold", external=True, default=1000, choices={500: "Half", 1000: "Full"}
        )
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


def test_co_running_timers_do_not_inflate_visited_set():
    """Two timers running at once keep the visited set small.

    Both start with In0 (T1 500 ms, T2 700 ms).  Each Done is a choice, so
    besides the 11 rows exact timing reaches the search also keeps the two
    where T2 finished first (any timing could do it: the over-approximation
    the black-box model makes), and nothing else.
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
    assert not isinstance(states, Intractable), f"visited set inflated to {states.estimated_space}"
    rows = [dict(row) for row in states]
    t2_first = [row for row in rows if row["T2_Done"] and not row["T1_Done"]]
    assert len(states) == 13
    assert {row["B1"] for row in t2_first} == {False, True}
    assert len(t2_first) == 2


class TestTimeEventDrum:
    """A time & event drum's timed steps are black-box ticks like any timer's:
    the search finds the step-3 hit, the PLC replays it, and how() keeps its
    prover context."""

    @staticmethod
    def _program(hit_reads_y3: bool) -> Program:
        from pyrung.core import time_event_drum

        with Program(strict=False) as logic:
            with Rung():
                time_event_drum(
                    outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                    presets=[30, None, 20],
                    events=[None, Bool("A"), Bool("B")],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("R"))
            with Rung(Bool("Done"), ~Bool("Y3")) if hit_reads_y3 else Rung(Bool("Done")):
                out(Bool("H"))
        return logic

    @pytest.mark.parametrize("hit_reads_y3", [False, True])
    def test_counterexample_replays(self, hit_reads_y3: bool) -> None:
        logic = self._program(hit_reads_y3)
        result = always(logic, ~Bool("H"))
        _assert_hit_replays(logic, result, "H")

    @pytest.mark.parametrize(
        "acc", [Int("Acc", min=0, max=5), Int("Acc", choices={0: "a", 1: "b", 2: "c"})]
    )
    def test_declared_acc_domain_still_reaches_the_timeout(self, acc: Int) -> None:
        """A declared domain on Acc does not hide the step-1 timeout: the tick
        that finishes the 1 s step is a choice, and the PLC replays it."""
        from pyrung.core import time_event_drum

        with Program(strict=False) as logic:
            with Rung():
                time_event_drum(
                    outputs=[Bool("Y1"), Bool("Y2")],
                    presets=[1, None],
                    events=[None, Bool("A", default=True)],
                    unit="sec",
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    accumulator=acc,
                    completion_flag=Bool("Done"),
                ).reset(Bool("R", readonly=True))
            with Rung(Bool("Y2")):
                out(Bool("H"))
        result = always(logic, ~Bool("H"))
        _assert_hit_replays(logic, result, "H")

    def test_how_keeps_its_prover_context(self) -> None:
        from pyrung.core.analysis.pilot.drive_setup import build_prover_context

        logic = self._program(False)
        plc = PLC(logic, dt=0.01)
        prover = build_prover_context(logic, dict(plc.state.tags))
        assert prover.nd_domains
        assert prover.key_config is not None
        assert "Step" in prover.key_config.stateful_names
        assert plc.how(Bool("Done"), max_scans=200).reachable


class TestDrumCompletion:
    """A completed drum turns its outputs off and keeps its flag on through a
    jump (only a reset clears it); a jog or its own step forward brings the
    outputs back.  A time drum with a jump times its steps again under that
    flag; its ticks are black boxes, so the search finds those steps and the
    PLC replays them."""

    @staticmethod
    def _time_drum(*, jump: bool) -> Program:
        from pyrung.core import time_drum

        with Program(strict=False) as logic:
            with Rung(Bool("Enable")):
                drum = time_drum(
                    outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                    presets=[200, 300, 500],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("Reset"))
                if jump:
                    drum.jump(Bool("Jump"), step=1)
        return logic

    def test_jumping_time_drum_times_its_steps_again(self) -> None:
        logic = self._time_drum(jump=True)
        # Reachable: complete, jump to step 1, time steps 1 and 2 again.
        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True})
        plc.run_for(1.1)
        assert plc.current_state.tags["Done"] is True
        plc.patch({"Jump": True})
        plc.step()
        plc.patch({"Jump": False})
        plc.run_for(0.6)
        tags = plc.current_state.tags
        assert (tags["Done"], tags["Step"], tags["Y3"]) == (True, 3, True)

        result = never(logic, Bool("Done"), Int("Step") == 3, Bool("Y3"))
        assert isinstance(result, Counterexample)
        tags = _replay_trace(logic, result.trace).current_state.tags
        assert (tags["Done"], tags["Step"], tags["Y3"]) == (True, 3, True)

    def test_flag_that_starts_on_reaches_step_two(self) -> None:
        """No jump, but the flag is on from the start, while the drum times
        steps 1 and 2: step 2 under the flag is reachable."""
        from pyrung.core import time_drum

        with Program(strict=False) as logic:
            with Rung(Bool("Enable")):
                time_drum(
                    outputs=[Bool("Y1"), Bool("Y2"), Bool("Y3")],
                    presets=[200, 300, 500],
                    unit="ms",
                    pattern=[[1, 0, 0], [0, 1, 0], [0, 0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done", default=True),
                ).reset(Bool("Reset"))
        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True})
        plc.run_for(0.25)
        tags = plc.current_state.tags
        assert (tags["Done"], tags["Step"], tags["Y2"]) == (True, 2, True)

        result = never(logic, Bool("Done"), Int("Step") == 2, Bool("Y2"))
        assert isinstance(result, Counterexample)
        tags = _replay_trace(logic, result.trace).current_state.tags
        assert (tags["Done"], tags["Step"], tags["Y2"]) == (True, 2, True)

    def test_another_step_writer_reaches_step_two_under_the_flag(self) -> None:
        from pyrung.core import time_drum

        with Program(strict=False) as logic:
            with Rung(Bool("Enable")):
                time_drum(
                    outputs=[Bool("Y1"), Bool("Y2")],
                    presets=[200, 300],
                    unit="ms",
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    accumulator=Int("Acc"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("Reset"))
            with Rung(Bool("Back")):
                copy(1, Int("Step"), oneshot=True)
        result = never(logic, Bool("Done"), Int("Step") == 2, Bool("Y2"))
        assert isinstance(result, Counterexample)
        tags = _replay_trace(logic, result.trace).current_state.tags
        assert (tags["Done"], tags["Step"], tags["Y2"]) == (True, 2, True)

    def test_time_drum_without_a_jump(self) -> None:
        logic = self._time_drum(jump=False)
        assert isinstance(never(logic, Bool("Y1"), Bool("Y2")), Proven)
        # Completed: every output off under the flag.
        assert isinstance(never(logic, Bool("Done"), Bool("Y3")), Proven)
        result = never(logic, Bool("Done"), Int("Step") == 3, ~Bool("Y3"))
        assert isinstance(result, Counterexample)
        tags = _replay_trace(logic, result.trace).current_state.tags
        assert (tags["Done"], tags["Step"], tags["Y3"]) == (True, 3, False)

    def test_completion_turns_the_outputs_off(self) -> None:
        """The drum's completing scan sets the flag and ``_drum_done`` and turns
        every output off: a completed drum never drives the last output."""
        from pyrung.core import time_drum

        En, Rst = Bool("En", external=True), Bool("Rst", external=True)
        with Program(strict=False) as logic:
            with Rung(En):
                time_drum(
                    outputs=[Bool("O1"), Bool("O2")],
                    presets=[300, 300],
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step", default=1),
                    accumulator=Int("DAcc"),
                    completion_flag=Bool("Flag"),
                ).reset(Rst)
            with Rung(rise(En)):
                latch(Bool("Seen"))
        assert isinstance(never(logic, Bool("Flag"), Bool("O2")), Proven)

    def test_event_drum_jump_leaves_a_completed_drum_off(self) -> None:
        from pyrung.core import event_drum

        with Program(strict=False) as logic:
            with Rung(Bool("Enable")):
                event_drum(
                    outputs=[Bool("Y1"), Bool("Y2")],
                    events=[Bool("E1"), Bool("E2")],
                    pattern=[[1, 0], [0, 1]],
                    current_step=Int("Step"),
                    completion_flag=Bool("Done"),
                ).reset(Bool("Reset")).jump(Bool("Jump"), step=1).jog(Bool("Jog"))

        result = never(logic, Bool("Enable"), Bool("Done"), Int("Step") == 1, ~Bool("Y1"))
        assert isinstance(result, Counterexample)
        tags = _replay_trace(logic, result.trace).current_state.tags
        assert (tags["Enable"], tags["Done"], tags["Step"], tags["Y1"]) == (True, True, 1, False)
        # Off by completion, never with both outputs on.
        assert isinstance(never(logic, Bool("Y1"), Bool("Y2")), Proven)
