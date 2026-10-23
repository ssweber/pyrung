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
    rise,
    system,
)
from pyrung.core.analysis.prove import (
    PENDING,
    Counterexample,
    Intractable,
    Proven,
    TraceStep,
    _classify_dimensions,
    always,
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

        result = always(logic, d0 < 1, max_states=10_000, depth_budget=20)

        assert isinstance(result, Counterexample)
        assert any(step.inputs.get("In0") is True for step in result.trace)


class TestOneShotProgressAbsorptionGaps:
    """Oneshot calc/copy accumulators must not be classified as constant-stride progress sources."""

    def test_oneshot_calc_accumulator_not_absorbed(self):
        """Oneshot calc(Step+1, Step) only fires on rising edges — not constant-stride."""
        Trigger = Bool("Trigger", external=True)
        Step = Int("Step")
        Done = Bool("Done")
        with Program(strict=False) as logic:
            with Rung(Trigger):
                calc(Step + 1, Step, oneshot=True)
            with Rung(Step == 3):
                latch(Done)

        _assert_soundness(logic, ~Done)

    def test_oneshot_copy_accumulator_not_absorbed(self):
        """Oneshot copy(Step+1, Step) — same gap via the CopyInstruction path."""
        Trigger = Bool("Trigger", external=True)
        Step = Int("Step")
        Done = Bool("Done")
        with Program(strict=False) as logic:
            with Rung(Trigger):
                copy(Step + 1, Step, oneshot=True)
            with Rung(Step == 3):
                latch(Done)

        _assert_soundness(logic, ~Done)


class TestCountDownAbsorptionGaps:
    """CountDown counters are excluded from all absorption paths.

    Verify that always() still reaches Done=True states and that
    intermediate-Acc-dependent outputs are reachable.
    """

    def test_count_down_done_is_reachable(self):
        """Basic count_down: always() should find the Done=True state."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDReset", external=True)
        counter = Counter.clone("CTD")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=5).reset(rst)
            with Rung(counter.Done):
                out(alarm)

        result = always(logic, ~alarm)
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

        result = always(logic, ~alarm)
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

    def test_large_preset_consumed_acc_gets_boundary_compressed_domain(self):
        """Consumed acc with preset > 32 uses boundary compression, not full range."""
        enable = Bool("Enable", external=True)
        gate = Bool("Gate", external=True)
        counter_a = Counter.clone("CA")
        counter_b = Counter.clone("CB")
        helper = Bool("Helper")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(counter_a, 100).reset(counter_a.Done)
            with Rung(counter_a.Done):
                out(helper)
            with Rung(gate):
                count_down(counter_b, preset=5).reset(helper)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        if "CA_Acc" in stateful:
            assert len(stateful["CA_Acc"]) <= 32

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

        result = always(logic, ~alarm)
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

        result = always(logic, ~complete)
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

        result = always(logic, ~complete)
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

        result = always(logic, ~active)
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

        result = always(logic, ~was_reset)
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


class TestBoundaryDomainCompression:
    """Verify _compressed_acc_boundary_domain fires for large-preset consumed accumulators."""

    def test_on_delay_non_owner_write_triggers_boundary_compression(self):
        """on_delay(preset=500) with Acc>=100 comparison and non-owner write.

        The non-zero copy blocks threshold absorption (_acc_has_only_owner_writes
        returns False for ON_DELAY kind).  The comparison puts the acc in
        consumed_accs.  No forbidden data reads, so _compressed_acc_boundary_domain
        compresses the 501-value domain to boundary values only.
        """
        ext = Bool("Ext", external=True)
        b0 = Bool("B0")
        t0 = Timer.clone("T0")

        with Program(strict=False) as logic:
            with Rung(ext):
                on_delay(t0, 500)
            with Rung(t0.Acc >= 100):
                out(b0)
            with Rung(b0):
                copy(50, t0.Acc)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result

        domain = stateful.get("T0_Acc")
        assert domain is not None, "T0_Acc should be stateful (consumed, not absorbed)"
        assert len(domain) < 32, (
            f"Domain should be boundary-compressed, got {len(domain)} values: {domain}"
        )
        assert 0 in domain, "Default value 0 must be in boundary domain"
        assert 100 in domain, "Threshold 100 must be in boundary domain"
        assert 500 in domain, "Preset 500 must be in boundary domain"

        states = reachable_states(
            logic, project=["B0", "T0_Done"], max_states=10_000, depth_budget=20
        )
        assert not isinstance(states, Intractable)
        assert frozenset({("B0", True), ("T0_Done", False)}) in states


def _assert_replays_to(logic: Program, result, tag_name: str) -> None:
    assert isinstance(result, Counterexample)
    assert _replay_trace(logic, result.trace).current_state.tags[tag_name] is True


class TestProgressAcrossCycles:
    """Hidden accumulators that advance once per multi-scan cycle.

    The state key hides an absorbed accumulator below its thresholds, and the
    hidden-event jump only advances it on a self-loop.  A count per pulse or
    per change, or a timer running beside a toggling bit, returns to a known
    key with more progress; that progress must not be dropped.
    """

    def test_change_counter(self):
        source = Int("ChgSource", external=True, choices={0: "a", 1: "b"}, default=0)
        last = Int("ChgLast")
        count = Int("ChgCount")
        hit = Bool("ChgHit")
        with Program(strict=False) as logic:
            with Rung(source != last):
                calc(count + 1, count)
                copy(source, last)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "ChgHit")

    def test_change_counter_on_a_copied_input(self):
        source = Int("ChgInSource", external=True, choices={0: "a", 1: "b"}, default=0)
        current = Int("ChgInCurrent")
        last = Int("ChgInLast")
        count = Int("ChgInCount")
        hit = Bool("ChgInHit")
        with Program(strict=False) as logic:
            with Rung():
                copy(source, current)
            with Rung(current != last):
                calc(count + 1, count)
                copy(current, last)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "ChgInHit")

    def test_minute_change_counter(self):
        current = Int("MinCurrent")
        last = Int("MinLast")
        count = Int("MinCount")
        hit = Bool("MinHit")
        with Program(strict=False) as logic:
            with Rung():
                copy(system.rtc.minute, current)
            with Rung(current != last):
                calc(count + 1, count)
                copy(current, last)
            with Rung(count >= 3):
                out(hit)
        assert isinstance(always(logic, ~hit), Counterexample)

    def test_rise_counter(self):
        pulse = Bool("RiseIn", external=True)
        count = Int("RiseCount")
        hit = Bool("RiseHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                calc(count + 1, count)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "RiseHit")

    def test_count_up_on_a_rising_edge(self):
        pulse = Bool("CtuIn", external=True)
        clear = Bool("CtuClear", external=True)
        counter = Counter.clone("CtuRise")
        hit = Bool("CtuHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                count_up(counter, preset=3).reset(clear)
            with Rung(counter.Done):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "CtuHit")

    def test_timer_beside_a_toggling_bit(self):
        enable = Bool("OscEnable", external=True)
        toggle = Bool("OscToggle")
        timer = Timer.clone("OscTimer")
        hit = Bool("OscHit")
        with Program(strict=False) as logic:
            with Rung(~toggle):
                out(toggle)
            with Rung(enable):
                on_delay(timer, preset=50)
            with Rung(timer.Done):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "OscHit")

    def test_large_preset_is_pumped_with_a_faithful_trace(self):
        pulse = Bool("PumpIn", external=True)
        count = Int("PumpCount")
        hit = Bool("PumpHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                calc(count + 1, count)
            with Rung(count >= 1000):
                out(hit)
        result = always(logic, ~hit)
        _assert_replays_to(logic, result, "PumpHit")
        assert isinstance(result, Counterexample)
        assert len(result.trace) == 2000
        assert result.caveats == ()

    def test_reachable_states_sees_the_pulsed_crossing(self):
        pulse = Bool("ReachIn", external=True)
        count = Int("ReachCount")
        hit = Bool("ReachHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                calc(count + 1, count)
            with Rung(count >= 5):
                out(hit)
        states = reachable_states(logic, project=["ReachHit"])
        assert states == frozenset(
            {frozenset({("ReachHit", False)}), frozenset({("ReachHit", True)})}
        )

    def test_reset_between_pulses_stays_proven(self):
        pulse = Bool("ResetIn", external=True)
        count = Int("ResetCount")
        hit = Bool("ResetHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                calc(count + 1, count)
            with Rung(~pulse):
                copy(0, count)
            with Rung(count >= 2):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

    def test_timer_reset_by_the_toggle_stays_proven(self):
        toggle = Bool("TogToggle")
        timer = Timer.clone("TogTimer")
        hit = Bool("TogHit")
        with Program(strict=False) as logic:
            with Rung(~toggle):
                out(toggle)
            with Rung(toggle):
                on_delay(timer, preset=30)
            with Rung(timer.Done):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

    def test_reset_inside_the_cycle_is_not_pumped(self):
        pulse = Bool("MaskIn", external=True)
        count = Int("MaskCount")
        hit = Bool("MaskHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                copy(0, count)
                calc(count + 1, count)
            with Rung(count >= 2):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

    def test_counter_beside_a_toggling_bit(self):
        enable = Bool("TogCntEnable", external=True)
        toggle = Bool("TogCntToggle")
        count = Int("TogCntCount")
        hit = Bool("TogCntHit")
        with Program(strict=False) as logic:
            with Rung(~toggle):
                out(toggle)
            with Rung(enable, toggle):
                calc(count + 1, count)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "TogCntHit")

    def test_change_counter_on_a_wide_copied_input(self):
        """The last-seen value is compared with a tag rewritten every scan,
        so its hidden value decides the next comparison: it stays exact."""
        source = Int("WideSource", external=True, choices={v: f"v{v}" for v in range(20)})
        current = Int("WideCurrent")
        last = Int("WideLast")
        count = Int("WideCount")
        hit = Bool("WideHit")
        with Program(strict=False) as logic:
            with Rung():
                copy(source, current)
            with Rung(current != last):
                calc(count + 1, count)
                copy(current, last)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "WideHit")

    def _counter_racing_a_timer(self, preset: int, *, ctu: bool, tag: str) -> tuple[Program, Bool]:
        arm = Bool(tag + "Arm", external=True)
        pulse = Bool(tag + "Pulse", external=True)
        armed = Bool(tag + "Armed")
        count = Int(tag + "Count")
        counter = Counter.clone(tag + "Ctu")
        timer = Timer.clone(tag + "Tmr")
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse), armed):
                if ctu:
                    count_up(counter, preset=5).reset(hit)
                else:
                    calc(count + 1, count)
            with Rung(arm):
                latch(armed)
            with Rung(armed):
                on_delay(timer, preset=preset)
            with Rung(counter.Done if ctu else count >= 3, ~timer.Done):
                out(hit)
        return logic, hit

    def test_pumping_carries_a_timer_running_beside_the_pulses(self):
        """The timer advances on every lap the trace repeats: it is pumped too,
        and its preset bounds the repeats (no unreplayable counterexample)."""
        logic, hit = self._counter_racing_a_timer(60, ctu=False, tag="RaceInt")
        assert isinstance(always(logic, ~hit), Proven)
        assert reachable_states(logic, project=["RaceIntHit"]) == frozenset(
            {frozenset({("RaceIntHit", False)})}
        )
        logic, hit = self._counter_racing_a_timer(100, ctu=True, tag="RaceCtu")
        assert isinstance(always(logic, ~hit), Proven)

    def test_pulses_that_beat_the_timer_still_count(self):
        logic, hit = self._counter_racing_a_timer(70, ctu=False, tag="RaceWin")
        _assert_replays_to(logic, always(logic, ~hit), "RaceWinHit")

    def test_huge_preset_trace_lists_the_cycle_once(self):
        pulse = Bool("HugeIn", external=True)
        clear = Bool("HugeClear", external=True)
        counter = Counter.clone("HugeCtu")
        hit = Bool("HugeHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                count_up(counter, preset=10**7).reset(clear)
            with Rung(counter.Done):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Counterexample)
        assert len(result.trace) < 100
        assert any("more times to replay it" in caveat for caveat in result.caveats)


class TestUnreadTimer:
    """A timer nothing reads (Done, accumulator, status bits) changes no other
    tag: it is left out of the key.  One read only by another instruction's
    condition is not unread."""

    def test_unread_timer_leaves_the_key(self):
        enable = Bool("SinkEn", external=True)
        sink = Timer.clone("SinkTmr")
        used = Timer.clone("SinkUsed")
        counter = Counter.clone("SinkCtu")
        hit = Bool("SinkHit")
        with Program(strict=False) as logic:
            with Rung():
                on_delay(sink, preset=1, unit="sec")
            with Rung():
                on_delay(used, preset=50)
            with Rung(enable):
                count_up(counter, preset=10).reset(used.Done)
            with Rung(counter.Acc >= 7):
                out(hit)
        result = always(logic, ~hit, _debug=True)
        assert isinstance(result, Proven)
        names = result._debug_context.stateful_names
        assert "SinkTmr_Done" not in names
        assert "SinkUsed_Done" in names
