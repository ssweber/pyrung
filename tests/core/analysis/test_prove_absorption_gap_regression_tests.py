"""Tests for the exhaustive verifier and state-space snapshot."""

from __future__ import annotations

import importlib

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Rung,
    TagType,
    Timer,
    branch,
    calc,
    call,
    copy,
    count_down,
    count_up,
    fall,
    latch,
    on_delay,
    out,
    reset,
    rise,
    subroutine,
    system,
)
from pyrung.core.analysis.prove import (
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
                count_down(counter, preset=-5).reset(rst)
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
                count_down(counter, preset=-5).reset(rst)
            with Rung(counter.Acc <= -3):
                out(warning)
            with Rung(counter.Done):
                out(alarm)

        result = always(logic, ~alarm)
        assert isinstance(result, Counterexample), (
            "count_down with consumed Acc: Done=True should be reachable"
        )

    def test_count_down_reset_via_threshold_comparison_keeps_a_bounded_acc(self):
        """Reset fed by a threshold comparison: the accumulator is state the
        search counts concretely, bounded between the reset and the preset."""
        enable = Bool("Enable", external=True)
        reset_from_threshold = Bool("ResetFromThreshold")
        counter = Counter.clone("ResetFedCtd")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=-5).reset(reset_from_threshold)
            with Rung(counter.Acc <= -3):
                out(reset_from_threshold)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert set(stateful["ResetFedCtd_Acc"]) <= set(range(-5, 1))

    def test_count_down_reset_via_data_copy_of_acc_blocks_absorption(self):
        """Reset fed by a data copy of the accumulator is NOT threshold-mediated."""
        enable = Bool("Enable", external=True)
        acc_mirror = Int("AccMirror")
        reset_helper = Bool("ResetHelper")
        counter = Counter.clone("DataFedCtd")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=-5).reset(reset_helper)
            with Rung():
                copy(counter.Acc, acc_mirror)
            with Rung(acc_mirror <= -3):
                out(reset_helper)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        assert "DataFedCtd_Acc" in stateful

    def test_large_preset_counter_acc_is_bounded_by_its_preset(self):
        """A counter reset by its own Done is counted concretely up to its preset."""
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
                count_down(counter_b, preset=-5).reset(helper)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful, _nd, _comb, _done_acc, _done_presets, _done_kinds = result
        if "CA_Acc" in stateful:
            assert max(stateful["CA_Acc"]) <= 100

    def test_count_down_intermediate_state_reachable(self):
        """count_down intermediate output (Acc-dependent) must be reachable."""
        enable = Bool("Enable", external=True)
        rst = Bool("CTDMidReset", external=True)
        counter = Counter.clone("CTDMid")
        midway = Bool("Midway")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_down(counter, preset=-10).reset(rst)
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


class TestConstantPresetCounter:
    """A count_up counter compared with its own constant preset."""

    def test_count_up_constant_preset_acc_lt_is_tracked(self):
        """count_up with constant preset and Acc < Preset: Done is a plain Bool
        and the accumulator is state (nothing is absorbed)."""
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
        assert stateful.get("ConstCtr_Done") == (False, True)
        assert "ConstCtr_Acc" in stateful

    def test_count_up_constant_preset_acc_le_not_same_boundary(self):
        """Acc <= Preset is NOT the same boundary as Acc < Preset; Done is still
        reached (ten edges, two scans each, within the default budget)."""
        enable = Bool("Enable", external=True)
        rst = Bool("LeCtrReset", external=True)
        c = Counter.clone("LeCtr")
        output = Bool("LeOutput")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=10).reset(rst)
            with Rung(c.Acc <= 10):
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
    """Accumulators that advance once per multi-scan cycle (a count per pulse
    or per change, a timer beside a toggling bit).  The search keys them
    concretely, so every count is a step: a threshold past depth_budget is a
    caveated Proven, never a silent one.
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

    def test_large_preset_past_the_budget_says_so(self):
        """A thousand pulses are two thousand scans: past the budget, the
        Proven carries the depth caveat."""
        pulse = Bool("PumpIn", external=True)
        count = Int("PumpCount")
        hit = Bool("PumpHit")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                calc(count + 1, count)
            with Rung(count >= 1000):
                out(hit)
        result = always(logic, ~hit)
        assert isinstance(result, Proven)
        assert any("depth_budget=50" in caveat for caveat in result.caveats)

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

    def test_timer_reset_by_the_toggle_is_unconfirmed(self):
        """Enabled every other scan, the timer restarts before 30 ms: that is
        timing, unconfirmed (not Proven) under black-box timers."""
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
        result = always(logic, ~hit)
        assert isinstance(result, Intractable)
        assert result.kind == "timing"
        assert "TogTimer_Done" in result.tags

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

    def test_pulses_racing_a_short_timer_are_unconfirmed(self):
        """Three pulses need six scans; a 60 ms timer (100 ms for the counter)
        finishes first on the PLC.  Only timing says so: unconfirmed, not
        Proven, and the hit row is over-approximated into the reachable set."""
        logic, hit = self._counter_racing_a_timer(60, ctu=False, tag="RaceInt")
        result = always(logic, ~hit)
        assert isinstance(result, Intractable)
        assert result.kind == "timing"
        assert reachable_states(logic, project=["RaceIntHit"]) == frozenset(
            {frozenset({("RaceIntHit", False)}), frozenset({("RaceIntHit", True)})}
        )
        logic, hit = self._counter_racing_a_timer(100, ctu=True, tag="RaceCtu")
        result = always(logic, ~hit)
        assert isinstance(result, Intractable)
        assert result.kind == "timing"

    def test_pulses_that_beat_the_timer_still_count(self):
        logic, hit = self._counter_racing_a_timer(70, ctu=False, tag="RaceWin")
        _assert_replays_to(logic, always(logic, ~hit), "RaceWinHit")

    def test_huge_preset_past_the_budget_says_so(self):
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
        assert isinstance(result, Proven)
        assert any("depth_budget=50" in caveat for caveat in result.caveats)


class TestSelfResettingTimerLaps:
    """A timer reset by its own Done is a pulse generator: every lap is a
    timer choice (it may finish on any enabled scan), and the counters
    counting laps count concretely; replay confirms what the search finds."""

    def _generator(self, tag: str, preset: int = 50, *, enable: Bool | None = None):
        timer = Timer.clone(tag + "Tmr")
        with Rung(enable, ~timer.Done) if enable is not None else Rung(~timer.Done):
            on_delay(timer, preset=preset)
        return timer

    def test_lap_counter_reaches_its_threshold(self):
        count = Int("LapCount")
        hit = Bool("LapHit")
        with Program(strict=False) as logic:
            timer = self._generator("Lap")
            with Rung(timer.Done):
                calc(count + 1, count)
            with Rung(count >= 3):
                out(hit)
        result = always(logic, ~hit)
        _assert_replays_to(logic, result, "LapHit")
        assert not result.caveats
        assert sum(step.scans for step in result.trace) == 17

    def test_count_up_and_rising_edge_count_laps(self):
        clear = Bool("LapCtuClear", external=True)
        counter = Counter.clone("LapCtu")
        hit = Bool("LapCtuHit")
        with Program(strict=False) as logic:
            timer = self._generator("LapCtu")
            with Rung(timer.Done):
                count_up(counter, preset=3).reset(clear)
            with Rung(counter.Done):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "LapCtuHit")

        count = Int("LapRiseCount")
        hit = Bool("LapRiseHit")
        with Program(strict=False) as logic:
            timer = self._generator("LapRise")
            with Rung(rise(timer.Done)):
                calc(count + 1, count)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "LapRiseHit")

    def test_gated_generator_counts_while_enabled(self):
        enable = Bool("LapGateEn", external=True)
        count = Int("LapGateCount")
        hit = Bool("LapGateHit")
        with Program(strict=False) as logic:
            timer = self._generator("LapGate", 30, enable=enable)
            with Rung(timer.Done):
                calc(count + 1, count)
            with Rung(count >= 3):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "LapGateHit")

    def test_laps_racing_a_timer(self):
        """Three 4-scan laps beat a 13-scan timer (a replaying counterexample);
        against an 11-scan one only timing decides: unconfirmed, not Proven."""
        for preset, wins in ((130, True), (110, False)):
            tag = f"LapRace{preset}"
            clear = Bool(tag + "Clear", external=True)
            counter = Counter.clone(tag + "Ctu")
            deadline = Timer.clone(tag + "Deadline")
            hit = Bool(tag + "Hit")
            with Program(strict=False) as logic:
                timer = self._generator(tag, 30)
                with Rung(timer.Done):
                    count_up(counter, preset=3).reset(clear)
                with Rung():
                    on_delay(deadline, preset=preset)
                with Rung(counter.Done, ~deadline.Done):
                    out(hit)
            result = always(logic, ~hit)
            if wins:
                _assert_replays_to(logic, result, hit.name)
            else:
                assert isinstance(result, Intractable)
                assert result.kind == "timing"

    def test_lap_counter_reset_or_stopped_stays_proven(self):
        count = Int("LapResetCount")
        hit = Bool("LapResetHit")
        with Program(strict=False) as logic:
            timer = self._generator("LapReset", 30)
            with Rung(timer.Done):
                calc(count + 1, count)
            with Rung(count >= 2):
                copy(0, count)
            with Rung(count >= 3):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)

        count = Int("LapStopCount")
        stop = Bool("LapStop")
        hit = Bool("LapStopHit")
        with Program(strict=False) as logic:
            timer = Timer.clone("LapStopTmr")
            with Rung(~timer.Done, ~stop):
                on_delay(timer, preset=30)
            with Rung(timer.Done):
                calc(count + 1, count)
            with Rung(count >= 2):
                latch(stop)
            with Rung(count >= 3):
                out(hit)
        assert isinstance(always(logic, ~hit), Proven)


class TestUnreadTimer:
    """A timer nothing reads (Done, accumulator, status bits) changes no other
    tag: it is left out of the key and its tick never branches.  One read
    only by another instruction's condition is not unread."""

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
        # Seven counts before the 50 ms reset is timing: unconfirmed, and it
        # names the timer that is read, not the unread one.
        assert isinstance(result, Intractable)
        assert result.kind == "timing"
        assert result.tags == ["SinkUsed_Done"]
        names = result._debug_context.stateful_names
        assert "SinkTmr_Done" not in names
        assert "SinkTmr_Acc" not in names
        assert "SinkUsed_Acc" in names
        model = result._debug_context.timer_model
        assert "SinkTmr_Acc" not in model.branch


class TestProgressMadeOnAnotherBranch:
    """A key reached again with hidden progress made earlier on another branch.

    A counter's value there can be ahead of the first node's (a pulse that came
    with the step that started the timer) or behind it (the first node took a
    shortcut that also counted); a timer can be behind (it started later).
    Neither is reachable from the first node, so the revisit is its own node.
    """

    def test_counter_ahead_wins_the_race(self):
        arm = Bool("AheadArm", external=True)
        pulse = Bool("AheadPulse", external=True)
        armed = Bool("AheadArmed")
        count = Int("AheadCount")
        timer = Timer.clone("AheadTmr")
        hit = Bool("AheadHit")
        with Program(strict=False) as logic:
            with Rung(arm):
                latch(armed)
            with Rung(armed):
                on_delay(timer, preset=1000)
            with Rung(rise(pulse), armed):
                calc(count + 1, count)
            with Rung(count >= 3, ~timer.Done):
                out(hit)
        result = always(logic, ~hit)
        _assert_replays_to(logic, result, "AheadHit")
        assert sum(step.scans for step in result.trace) == 5

    def _shortcut(self, tag: str, *, lock_first: bool) -> tuple[Program, Bool]:
        """Path B arms in one step and counts N once; path A takes two steps."""
        a = Bool(tag + "A", external=True)
        b = Bool(tag + "B", external=True)
        x = Bool(tag + "X", external=True)
        armed = Bool(tag + "S")
        pre = Bool(tag + "P")
        n = Int(tag + "N")
        m = Int(tag + "M")
        lock = Bool(tag + "K")
        hit = Bool(tag + "H")
        with Program(strict=False) as logic:
            with Rung(pre):
                latch(armed)
                reset(pre)
            with Rung(b, ~armed, ~pre):
                latch(armed)
                calc(n + 1, n)
            with Rung(a, ~armed, ~pre):
                latch(pre)
            with Rung(armed, rise(x)):
                calc(n + 1, n)
                calc(m + 1, m)
            if not lock_first:
                with Rung(m >= 3, ~lock):
                    out(hit)
            with Rung(n >= 3):
                latch(lock)
            if lock_first:
                with Rung(m >= 3, ~lock):
                    out(hit)
        return logic, hit

    def test_counter_behind_has_room_left(self):
        """Only the longer path, with N behind, counts M to 3 before N locks out."""
        logic, hit = self._shortcut("Behind", lock_first=False)
        _assert_replays_to(logic, always(logic, ~hit), "BehindH")
        logic, hit = self._shortcut("BehindLock", lock_first=True)
        assert isinstance(always(logic, ~hit), Proven)

    def test_timer_behind_has_time_left(self):
        """A detour arms the timer later: the pulses then beat it."""
        a = Bool("LateA", external=True)
        b = Bool("LateB", external=True)
        c = Bool("LateC", external=True)
        pulse = Bool("LateY", external=True)
        armed = Bool("LateL")
        parked = Bool("LateP")
        count = Int("LateN")
        timer = Timer.clone("LateTmr")
        hit = Bool("LateH")
        with Program(strict=False) as logic:
            with Rung(rise(pulse), armed):
                calc(count + 1, count)
            with Rung(a, ~armed, ~parked):
                latch(armed)
            with Rung(armed):
                on_delay(timer, preset=60)
            with Rung(count >= 3, ~timer.Done):
                out(hit)
            with Rung(b, ~armed, ~parked):
                latch(parked)
            with Rung(parked, c):
                latch(armed)
                reset(parked)
        _assert_replays_to(logic, always(logic, ~hit), "LateH")

    def test_race_carried_across_rungs_by_latches(self):
        """The head-start race with each crossing latched on its own rung."""
        arm = Bool("XrA", external=True)
        pulse = Bool("XrY", external=True)
        armed = Bool("XrL")
        count = Int("XrN")
        timer = Timer.clone("XrTmr")
        counted = Bool("XrX")
        late = Bool("XrLate")
        hit = Bool("XrH")
        with Program(strict=False) as logic:
            with Rung(arm):
                latch(armed)
            with Rung(armed):
                on_delay(timer, preset=60)
            with Rung(rise(pulse), armed):
                calc(count + 1, count)
            with Rung(count >= 3):
                latch(counted)
            with Rung(timer.Done):
                latch(late)
            with Rung(counted, ~late):
                out(hit)
        result = always(logic, ~hit)
        _assert_replays_to(logic, result, "XrH")
        assert sum(step.scans for step in result.trace) == 5

    @pytest.mark.parametrize(("steps", "lock_at"), [(3, 3), (4, 4)])
    def test_one_counter_races_a_visible_chain(self, steps: int, lock_at: int):
        """No second accumulator: the pulses that count N also walk a latch chain.

        Path B arms in one step and counts N once, path A takes two steps and
        does not.  Only path A, with N behind, reaches the end of the chain
        before N locks it out.
        """
        tag = f"Chain{steps}"
        a = Bool(tag + "A", external=True)
        b = Bool(tag + "B", external=True)
        x = Bool(tag + "X", external=True)
        armed = Bool(tag + "S")
        pre = Bool(tag + "P")
        n = Int(tag + "N")
        lock = Bool(tag + "K")
        hit = Bool(tag + "H")
        stages = [Bool(f"{tag}C{i}") for i in range(steps)]
        with Program(strict=False) as logic:
            with Rung(pre):
                latch(armed)
                reset(pre)
            with Rung(b, ~armed, ~pre):
                latch(armed)
                calc(n + 1, n)
            with Rung(a, ~armed, ~pre):
                latch(pre)
            with Rung(armed, rise(x)):
                calc(n + 1, n)
            for i in range(steps - 1, -1, -1):
                with Rung(rise(x), stages[i - 1] if i else armed, ~stages[i]):
                    latch(stages[i])
            with Rung(stages[-1], ~lock):
                out(hit)
            with Rung(n >= lock_at):
                latch(lock)
        _assert_replays_to(logic, always(logic, ~hit), tag + "H")


class TestCounterRacingATimerBehindAGuard:
    """A count of Y's rises read only behind ``Rung(Req != 0): call(run)``
    races a timer the request starts: H latches if the count reaches 2 before
    the timer is done.  With a 20 ms timer only timing decides, so no variant
    is ever Proven: one the PLC reaches is a replaying counterexample, the
    rest are unconfirmed.  (The exact-timing layer proved the guarded ones by
    a dead-guard analysis that is gone.)
    """

    def _program(self, tag: str, variant: str, preset: int = 20) -> tuple[Program, Bool]:
        go = Bool(tag + "Go", external=True)
        y = Bool(tag + "Y", external=True)
        cancel = Bool(tag + "Cx", external=True)
        table = Block(tag + "B", TagType.INT, 1, 4)
        req = table[1] if variant.startswith("indirect") else Int(tag + "Req")
        n = Int(tag + "N")
        hit = Bool(tag + "H")
        timer = Timer.clone(tag + "T")
        ptr = Int(tag + "P")
        with Program(strict=False) as logic:
            if variant == "no_reset":
                with Rung(go, req == 0):
                    copy(1, req)
            elif variant == "reset_elsewhere":
                with Rung(go, req == 0):
                    copy(1, req)
                with Rung(go, y):
                    copy(0, n)
            else:
                with Rung(go, req == 0):
                    copy(1, req)
                    copy(0, n)
            with Rung(cancel):
                copy(0, req)
            if variant.startswith("indirect"):
                with Rung():
                    copy(1 if variant == "indirect_hits" else 3, ptr)
                with Rung(cancel, y):
                    copy(1, table[ptr])
            with Rung(req != 0):
                on_delay(timer, preset=preset)
            if variant == "in_branch":
                with Rung(go):
                    with branch(req != 0):
                        call(tag + "Run")
                with Rung(~go, req != 0):
                    call(tag + "Run")
            elif variant == "unguarded":
                with Rung():
                    call(tag + "Run")
            else:
                with Rung(req != 0):
                    call(tag + "Run")
            if variant == "read_outside":
                with Rung(n >= 1, req == 0, y):
                    copy(0, n)
            with subroutine(tag + "Run"):
                with Rung(rise(y)):
                    calc(n + 1, n)
                with Rung(n >= 2, ~timer.Done):
                    latch(hit)
                with Rung(n >= 2):
                    copy(0, req)
                    copy(0, n)
        return logic, hit

    @pytest.mark.parametrize(
        ("variant", "preset", "confirmed"),
        [
            ("plain", 20, False),
            ("indirect_safe", 20, False),
            ("no_reset", 20, False),
            ("reset_elsewhere", 30, False),
            ("indirect_hits", 20, False),
            # Called every scan, the subroutine counts while the timer is idle
            # (Req == 0): the hit needs no timer choice and replays.
            ("unguarded", 20, True),
            ("in_branch", 20, False),
            ("read_outside", 20, False),
        ],
    )
    def test_race_with_a_short_timer_is_never_proven(
        self, variant: str, preset: int, confirmed: bool
    ):
        tag = "Dg" + "".join(part.title() for part in variant.split("_"))
        logic, hit = self._program(tag, variant, preset)
        result = always(logic, ~hit)
        if confirmed:
            _assert_replays_to(logic, result, tag + "H")
        else:
            assert isinstance(result, Intractable), result
            assert result.kind == "timing"
            assert tag + "T_Done" in result.tags

    @pytest.mark.parametrize("variant", ["plain", "indirect_safe"])
    def test_slow_timer_loses_the_race(self, variant: str):
        tag = "DgSlow" + "".join(part.title() for part in variant.split("_"))
        logic, hit = self._program(tag, variant, preset=40)
        _assert_replays_to(logic, always(logic, ~hit), tag + "H")


class TestEdgeInAnInstructionCondition:
    """An input whose rise is read only by an instruction's own condition
    (``count_up(...).down(rise(Y))``) is edge-bearing: its previous value
    stays in the key, and its edge is never dead."""

    def test_up_down_counter_with_a_rising_down_edge(self):
        x = Bool("UdX", external=True)
        y = Bool("UdY", external=True)
        l1 = Bool("UdL1")
        l2 = Bool("UdL2")
        counter = Counter.clone("UdCtr")
        hit = Bool("UdHit")
        with Program(strict=False) as logic:
            # Counted up on x's rises (on y's, the up and down edges would
            # always land on one scan and cancel).
            with Rung(x):
                count_up(counter, preset=5).down(rise(y)).reset(l2)
            with Rung(~y, ~l1):
                reset(l2)
            with Rung(fall(x), y):
                reset(l1)
            with Rung(~counter.Done):
                out(l1)
            with Rung(counter.Acc >= 5, y):
                out(hit)
        _assert_replays_to(logic, always(logic, ~hit), "UdHit")


class TestAccumulatorReadOutsideTheProgram:
    """A callable property or a projection reads concrete tag values: an
    accumulator it reads is tracked exactly, not behind a threshold vector or
    a Done state, whose representative value is all a callable would see."""

    def _level_counter(self, tag: str):
        enable = Bool(tag + "En", external=True)
        count = Int(tag + "N")
        hit = Bool(tag + "Hit")
        with Program(strict=False) as logic:
            with Rung(enable):
                calc(count + 1, count)
            with Rung(count >= 100):
                out(hit)
        return logic, count

    def test_callable_reads_an_integer_counter(self):
        logic, count = self._level_counter("CallN")
        result = always(logic, lambda s: s["CallNN"] != 50)
        assert isinstance(result, Counterexample)
        assert _replay_trace(logic, result.trace).current_state.tags["CallNN"] == 50

    def test_callable_reads_counter_and_timer_accumulators(self):
        pulse = Bool("CallCtuIn", external=True)
        clear = Bool("CallCtuClr", external=True)
        counter = Counter.clone("CallCtu")
        with Program(strict=False) as logic:
            with Rung(rise(pulse)):
                count_up(counter, preset=6).reset(clear)
        for scope in (None, ["CallCtu_Acc"]):
            result = always(logic, lambda s: s["CallCtu_Acc"] != 3, scope=scope)
            assert isinstance(result, Counterexample)
            assert _replay_trace(logic, result.trace).current_state.tags["CallCtu_Acc"] == 3

        enable = Bool("CallTonEn", external=True)
        timer = Timer.clone("CallTon")
        with Program(strict=False) as logic:
            with Rung(enable):
                on_delay(timer, preset=100)
        result = always(logic, lambda s: s["CallTon_Acc"] != 50)
        assert isinstance(result, Counterexample)
        assert _replay_trace(logic, result.trace).current_state.tags["CallTon_Acc"] == 50

    def test_unbounded_counter_read_without_a_comparison_is_not_proven(self):
        """In the cone of the projection or of the callable's scope the
        counter is compared nowhere: it has no domain, so not Proven."""
        logic, _count = self._level_counter("ProjN")
        assert isinstance(reachable_states(logic, project=["ProjNN"]), Intractable)
        assert isinstance(always(logic, lambda s: s["ProjNN"] != 50, scope=["ProjNN"]), Intractable)


class TestTimerTowardADynamicPreset:
    """A timer toward a tag preset (3, or 5 while X), beside a toggling bit,
    compared past it (Acc >= 40) on X's rise.  The search lets the tick land
    there; whether a scan sequence at a fixed dt reaches it with X rising is
    timing the guided replay does not find: unconfirmed, never Proven."""

    def test_accumulator_threshold_past_a_crossed_tag_preset(self):
        x = Bool("DynX", external=True)
        clear = Bool("DynClr", external=True)
        toggle = Bool("DynTog")
        cleared = Bool("DynL1")
        stop = Bool("DynL2")
        preset = Int("DynPre", retentive=True)
        timer = Timer.clone("DynTmr")
        hit = Bool("DynHit")
        with Program(strict=False) as logic:
            with Rung(toggle):
                reset(cleared)
            with Rung(toggle):
                copy(3, preset)
            with Rung(~toggle):
                out(toggle)
            with Rung(~stop):
                on_delay(timer, preset=preset).reset(clear)
            with Rung(x):
                copy(5, preset)
            with Rung(rise(x), timer.Acc >= 40):
                out(hit)
        result = always(logic, ~hit)
        assert not isinstance(result, Proven)
        if isinstance(result, Counterexample):
            _assert_replays_to(logic, result, "DynHit")
        else:
            assert isinstance(result, Intractable)
            assert result.kind == "timing"
