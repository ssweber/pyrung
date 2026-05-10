"""BFS model checker soundness coverage matrix.

Tests organized by subsystem interaction.  Each section header names the
primary optimizations under test.  Numbering in docstrings maps back to
the audit document (scratchpad/bfs-soundness-tests.md) and the original
gap analysis (scratchpad/adversarial-bfs-plan.md).

Every test should ideally pass in 3-way oracle mode (interpreted,
compiled, BFS all agree).  Concrete-PLC validation tests are included
alongside prove() tests where the expected behavior is non-obvious.
"""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Block,
    Bool,
    Counter,
    Int,
    Or,
    Program,
    Rung,
    TagType,
    Timer,
    blockcopy,
    calc,
    copy,
    count_up,
    fill,
    latch,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.prove import (
    Counterexample,
    Intractable,
    Proven,
    _classify_dimensions,
    prove,
    reachable_states,
)


def _replay_trace(program, trace):
    plc = PLC(program, dt=0.010)
    for step in trace:
        plc.patch(step.inputs)
        for _ in range(step.scans):
            plc.step()
    return plc


def _assert_trace_replays(program, result, violation_tag_name):
    """Replay a Counterexample trace on a concrete PLC and verify the violation."""
    if result.caveats:
        return
    plc = _replay_trace(program, result.trace)
    assert plc.current_state.tags[violation_tag_name] is True, (
        f"Trace replay did not reproduce {violation_tag_name}=True "
        f"(got {plc.current_state.tags.get(violation_tag_name)})"
    )


# ===================================================================
# Threshold Absorption
#
# Jumps counters/timers directly to comparison boundaries instead of
# stepping.  Risks: missing boundaries, assuming monotonicity when
# conditional resets exist.
# ===================================================================


class TestThresholdAbsorptionIntermediateValues:
    """Gap 1: Timer/counter fast-forward must not skip intermediate-
    value-dependent logic."""

    def test_equ_intermediate_value_fires_alarm(self):
        """EQU(T1.ACC, 50) with preset=100 — Alarm at ACC=50 is reachable."""
        run = Bool("Run", external=True)
        t = Timer.clone("T1")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t, preset=100)
            with Rung(t.Acc == 50):
                latch(alarm)

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"ACC=50 is reachable but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")

    def test_multi_threshold_alarm_bands(self):
        """Warning at ACC>40, trip at ACC>80, both on the same timer."""
        run = Bool("Run", external=True)
        t = Timer.clone("BandTmr")
        warning = Bool("Warning")
        trip = Bool("Trip")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t, preset=100)
            with Rung(t.Acc > 40):
                latch(warning)
            with Rung(t.Acc > 80):
                latch(trip)

        states = reachable_states(logic, project=["Warning", "Trip"], depth_budget=10)
        assert not isinstance(states, Intractable), (
            f"multi-threshold timer should be tractable: {states}"
        )
        assert frozenset({("Warning", True), ("Trip", True)}) in states
        assert frozenset({("Warning", True), ("Trip", False)}) in states

    def test_counter_80_percent_warning(self):
        """Counter with warning at 80% (Acc>=8) of preset=10 and trip at Done."""
        enable = Bool("Enable", external=True)
        rst = Bool("Rst", external=True)
        c = Counter.clone("AlarmCtr")
        warning = Bool("Warning80")
        trip = Bool("Trip100")

        with Program(strict=False) as logic:
            with Rung(enable):
                count_up(c, preset=10).reset(rst)
            with Rung(c.Acc >= 8):
                latch(warning)
            with Rung(c.Done):
                latch(trip)

        result = prove(logic, ~trip, depth_budget=10)
        assert isinstance(result, Counterexample), "trip at Done should be reachable"
        _assert_trace_replays(logic, result, "Trip100")
        result2 = prove(logic, ~warning, depth_budget=10)
        assert isinstance(result2, Counterexample), "warning at Acc>=8 should be reachable"
        _assert_trace_replays(logic, result2, "Warning80")


class TestThresholdAbsorptionIndirectCopy:
    """Test 1: Comparison against a copy of the timer accumulator, not
    the accumulator directly.  The verifier must propagate the threshold
    backward through the copy instruction."""

    def test_shadow_copy_of_timer_acc_fires_midpoint(self):
        run = Bool("Run", external=True)
        t1 = Timer.clone("ShadowTmr")
        shadow = Int("Shadow")
        midpoint = Bool("MidpointFlag")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t1, preset=100)
            with Rung():
                copy(t1.Acc, shadow)
            with Rung(shadow >= 50):
                out(midpoint)

        result = prove(logic, ~midpoint, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"MidpointFlag should fire when T1.Acc reaches 50 via Shadow, "
            f"got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "MidpointFlag")

    def test_shadow_copy_concrete_agrees(self):
        run = Bool("Run", external=True)
        t1 = Timer.clone("ShadowTmrV")
        shadow = Int("Shadow")
        midpoint = Bool("MidpointFlag")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t1, preset=100)
            with Rung():
                copy(t1.Acc, shadow)
            with Rung(shadow >= 50):
                out(midpoint)

        plc = PLC(logic, dt=1.0)
        plc.force("Run", True)
        for _ in range(110):
            plc.step()
        assert plc.current_state.tags["MidpointFlag"] is True


class TestThresholdAbsorptionConditionalResets:
    """Gap 8: Self-resetting counters (sawtooth patterns) must not be
    hidden by threshold absorption."""

    def test_sawtooth_counter_stopped_reachable(self):
        """calc(C + 1, C) resets at C>=50 — Stopped must be reachable."""
        enable = Bool("Enable", external=True)
        c = Int("SawtoothC")
        stopped = Bool("Stopped")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(c + 1, c)
            with Rung(c >= 50):
                copy(0, c)
                latch(stopped)

        plc = PLC(logic, dt=0.010)
        plc.force("Enable", True)
        for _ in range(55):
            plc.step()
        assert plc.current_state.tags["Stopped"] is True, (
            "concrete PLC should reach Stopped after sawtooth reset"
        )

        result = prove(logic, ~stopped, depth_budget=100)
        assert isinstance(result, Counterexample), (
            f"Stopped should be reachable, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Stopped")

    def test_modular_counter_cycles_back_to_zero(self):
        """Counter that resets at N and cycles — zero-after-counting is reachable."""
        enable = Bool("Enable", external=True)
        c = Int("ModularC")
        counting = Bool("Counting")
        cycled = Bool("Cycled")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(c + 1, c)
            with Rung(c > 0):
                latch(counting)
            with Rung(c >= 10):
                copy(0, c)
            with Rung(counting, c == 0):
                latch(cycled)

        plc = PLC(logic, dt=0.010)
        plc.force("Enable", True)
        for _ in range(15):
            plc.step()
        assert plc.current_state.tags["Cycled"] is True, (
            "concrete PLC should detect cycle-back-to-zero"
        )

        result = prove(logic, ~cycled, depth_budget=100)
        assert isinstance(result, Counterexample), (
            f"Cycled should be reachable via modular counter reset, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Cycled")

    def test_watchdog_timer_style_reset(self):
        """Watchdog: counter increments, input resets it — timeout only if input absent."""
        tick = Bool("Tick", external=True)
        pet = Bool("Pet", external=True)
        c = Int("WatchdogC")
        timeout = Bool("Timeout")

        with Program(strict=False) as logic:
            with Rung(tick):
                calc(c + 1, c)
            with Rung(pet):
                copy(0, c)
            with Rung(c >= 20):
                latch(timeout)

        result = prove(logic, ~timeout, depth_budget=50)
        assert isinstance(result, Counterexample), (
            "watchdog timeout should be reachable when pet is absent"
        )
        _assert_trace_replays(logic, result, "Timeout")


class TestThresholdAbsorptionRealAccumulator:
    """T-1: Float arithmetic accumulates rounding error.  Does the
    fast-forward landing state match concrete execution?"""

    def test_real_accumulator_fires_alarm(self):
        from pyrung.core import Real

        enable = Bool("Enable", external=True)
        x = Real("X")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(x + 0.1, x)
            with Rung(x >= 1.0):
                out(alarm)

        plc = PLC(logic, dt=0.010)
        plc.force("Enable", True)
        for _ in range(15):
            plc.step()
        concrete_alarm = plc.current_state.tags["Alarm"]

        result = prove(logic, ~alarm, depth_budget=20, max_states=10_000)
        if isinstance(result, Proven) and not result.caveats:
            assert not concrete_alarm, (
                "prove says Proven but concrete PLC fires Alarm — soundness bug"
            )
        elif isinstance(result, Counterexample):
            pass
        # Intractable or Proven-with-caveats is acceptable for Real


# ===================================================================
# Backward Propagation
#
# Traces comparisons backward through arithmetic/copies to seed input
# domains.  Risks: can't invert all operations, may not follow chains
# far enough.
# ===================================================================


class TestBackwardPropagationNumericEnumeration:
    """Gap 2: Unbounded numeric external inputs must not be silently
    under-enumerated."""

    def test_unbounded_int_inputs_are_intractable(self):
        """Two unconstrained external Ints — should be Intractable."""
        sensor = Int("Sensor", external=True)
        setpoint = Int("Setpoint", external=True)
        valve = Bool("Valve")

        with Program(strict=False) as logic:
            with Rung(sensor > setpoint):
                out(valve)

        result = _classify_dimensions(logic)
        assert isinstance(result, Intractable), "unconstrained Int × Int should be Intractable"

    def test_comparison_literal_creates_bounded_domain(self):
        """GRT(Sensor, 100) with min/max Sensor — should be tractable."""
        sensor = Int("Sensor", external=True, min=0, max=200)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(sensor > 100):
                out(alarm)

        result = prove(logic, ~alarm, depth_budget=5)
        assert isinstance(result, Counterexample), (
            "Sensor > 100 should be reachable with min=0, max=200"
        )
        _assert_trace_replays(logic, result, "Alarm")

    def test_single_unconstrained_int_is_intractable(self):
        """Single unconstrained external Int compared to literal."""
        sensor = Int("Sensor", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(sensor > 9999):
                out(alarm)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            assert "Sensor" in result.tags
        else:
            result2 = prove(logic, ~alarm, depth_budget=5)
            assert isinstance(result2, Counterexample), (
                "Sensor > 9999 must be reachable if domain was inferred"
            )
            _assert_trace_replays(logic, result2, "Alarm")


class TestBackwardPropagationNonInvertible:
    """Test 2: Sensor * Sensor can't be uniquely inverted.  The verifier
    must still find an input that satisfies the comparison."""

    def test_squared_comparison_finds_counterexample(self):
        sensor = Int("Sensor", external=True, min=0, max=20)
        squared = Int("Squared")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                calc(sensor * sensor, squared)
            with Rung(squared >= 100):
                out(alarm)

        result = prove(logic, ~alarm, depth_budget=5)
        assert isinstance(result, Counterexample), (
            f"Alarm should fire when Sensor>=10, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")

    def test_squared_concrete_agrees(self):
        sensor = Int("Sensor", external=True, min=0, max=20)
        squared = Int("Squared")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                calc(sensor * sensor, squared)
            with Rung(squared >= 100):
                out(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Sensor": 10})
        plc.step()
        assert plc.current_state.tags["Alarm"] is True


class TestBackwardPropagationMultiHop:
    """Test 3: Boundary value 85 must propagate back through +10 then
    through copy to seed Level's domain with 75."""

    def test_two_hop_chain_finds_boundary(self):
        level = Int("Level", external=True, min=0, max=100)
        stored = Int("Stored")
        shifted = Int("Shifted")
        high = Bool("High")

        with Program(strict=False) as logic:
            with Rung():
                copy(level, stored)
            with Rung():
                calc(stored + 10, shifted)
            with Rung(shifted >= 85):
                out(high)

        result = prove(logic, ~high, depth_budget=5)
        assert isinstance(result, Counterexample), (
            f"High should fire when Level>=75, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "High")

    def test_two_hop_concrete_agrees(self):
        level = Int("Level", external=True, min=0, max=100)
        stored = Int("Stored")
        shifted = Int("Shifted")
        high = Bool("High")

        with Program(strict=False) as logic:
            with Rung():
                copy(level, stored)
            with Rung():
                calc(stored + 10, shifted)
            with Rung(shifted >= 85):
                out(high)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Level": 75})
        plc.step()
        assert plc.current_state.tags["High"] is True


class TestBackwardPropagationElisionPromotion:
    """Test 11: When elision promotes a tag from auto-elided to stateful,
    backward propagation must seed its domain."""

    def test_conditional_write_promotes_to_stateful(self):
        mode = Int("Mode", external=True, min=0, max=20)
        value = Int("Value")
        result_tag = Bool("Result")

        with Program(strict=False) as logic:
            with Rung(mode >= 5):
                copy(mode, value)
            with Rung(value >= 10):
                out(result_tag)

        result = prove(logic, ~result_tag, depth_budget=20)
        assert isinstance(result, Counterexample), (
            f"Result should fire at Mode=10, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Result")

    def test_conditional_write_concrete_agrees(self):
        mode = Int("Mode", external=True, min=0, max=20)
        value = Int("Value")
        result_tag = Bool("Result")

        with Program(strict=False) as logic:
            with Rung(mode >= 5):
                copy(mode, value)
            with Rung(value >= 10):
                out(result_tag)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Mode": 10})
        plc.step()
        assert plc.current_state.tags["Result"] is True


class TestBackwardPropagationTagVsTag:
    """Test 12: Timer accumulator compared against a preset tag set by
    copying an external input.  Propagation must follow the chain back."""

    def test_timer_acc_vs_external_limit(self):
        run = Bool("Run", external=True)
        setting = Int("Setting", external=True, min=0, max=500)
        limit = Int("Limit")
        t1 = Timer.clone("LimitTmr")
        reached = Bool("Reached")

        with Program(strict=False) as logic:
            with Rung():
                copy(setting, limit)
            with Rung(run):
                on_delay(t1, preset=1000)
            with Rung(t1.Acc >= limit):
                out(reached)

        result = prove(logic, ~reached, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"Reached should fire when T1.Acc >= Setting, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Reached")


class TestBackwardPropagationWordBitwise:
    """T-2: Backward propagation can invert addition.  Can it invert
    a bitwise AND?"""

    def test_bitwise_and_finds_counterexample(self):
        from pyrung.core import Word

        mask = Word("Mask", external=True, min=0, max=255)
        masked = Word("Masked")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                calc(mask & 0x00FF, masked)
            with Rung(masked >= 16):
                out(flag)

        result = prove(logic, ~flag, depth_budget=5)
        assert isinstance(result, Counterexample), (
            f"Flag should fire when low byte of Mask >= 16, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Flag")

    def test_bitwise_and_concrete_agrees(self):
        from pyrung.core import Word

        mask = Word("Mask", external=True, min=0, max=255)
        masked = Word("Masked")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                calc(mask & 0x00FF, masked)
            with Rung(masked >= 16):
                out(flag)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Mask": 32})
        plc.step()
        assert plc.current_state.tags["Flag"] is True


class TestBackwardPropagationIntEquality:
    """T-4: An external Int with an equality check.  Does backward
    propagation seed the exact value?"""

    def test_equality_seeds_exact_value(self):
        selector = Int("Selector", external=True, min=0, max=100)
        match = Bool("Match")

        with Program(strict=False) as logic:
            with Rung(selector == 42):
                out(match)

        result = prove(logic, ~match, depth_budget=5)
        assert isinstance(result, Counterexample), (
            f"Match should fire at Selector=42, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Match")

    def test_equality_concrete_agrees(self):
        selector = Int("Selector", external=True, min=0, max=100)
        match = Bool("Match")

        with Program(strict=False) as logic:
            with Rung(selector == 42):
                out(match)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Selector": 42})
        plc.step()
        assert plc.current_state.tags["Match"] is True
        plc.patch({"Selector": 41})
        plc.step()
        assert plc.current_state.tags["Match"] is False


# ===================================================================
# Fast-Forward / Interleaving
#
# Skips timer scans, branching at interesting events.  Risks: missing
# combinations where inputs change mid-accumulation.
# ===================================================================


class TestFastForwardInputInterleaving:
    """Gap 5: Inputs that latch during the timer accumulation window
    must compose with the timer-done state."""

    def test_timer_plus_independent_latch_hazard_reachable(self):
        """Timer done + latched input = hazard; must be found by prove."""
        run = Bool("Run", external=True)
        trigger = Bool("Trigger", external=True)
        t = Timer.clone("HazardTmr")
        latch_tag = Bool("HazardLatch")
        hazard = Bool("Hazard")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t, preset=1000)
            with Rung(rise(trigger)):
                latch(latch_tag)
            with Rung(t.Done, latch_tag):
                out(hazard)

        result = prove(logic, ~hazard, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"timer done + latch should be reachable, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Hazard")

    def test_timer_plus_counter_interleaving(self):
        """Timer done + counter done from independent inputs."""
        run_tmr = Bool("RunTmr", external=True)
        run_ctr = Bool("RunCtr", external=True)
        rst_ctr = Bool("RstCtr", external=True)
        t = Timer.clone("InterleaveTmr")
        c = Counter.clone("InterleaveCtr")
        both_done = Bool("BothDone")

        with Program(strict=False) as logic:
            with Rung(run_tmr):
                on_delay(t, preset=100)
            with Rung(run_ctr):
                count_up(c, preset=5).reset(rst_ctr)
            with Rung(t.Done, c.Done):
                out(both_done)

        result = prove(logic, ~both_done, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "timer done + counter done interleaving should be reachable"
        )
        _assert_trace_replays(logic, result, "BothDone")

    def test_edge_during_timer_window_latches_then_timer_fires(self):
        """Trigger edge at any point during accumulation, then timer completes."""
        run = Bool("Run", external=True)
        trigger = Bool("Trigger", external=True)
        t = Timer.clone("EdgeWindowTmr")
        armed = Bool("Armed")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t, preset=500)
            with Rung(rise(trigger)):
                latch(armed)
            with Rung(armed, t.Done):
                latch(output)

        result = prove(logic, ~output, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "edge during timer window must compose with done state"
        )
        _assert_trace_replays(logic, result, "Output")


class TestFastForwardElisionTimerPath:
    """Test 6: Temp looks scan-local but its value at the scan when
    T1.Done fires determines the outcome."""

    def test_timer_gated_copy_not_elidable(self):
        sensor = Int("Sensor", external=True, min=0, max=100)
        run = Bool("Run", external=True)
        t1 = Timer.clone("ElisionTmr")
        temp = Int("Temp")
        saved = Int("Saved")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung():
                calc(sensor + 10, temp)
            with Rung(run):
                on_delay(t1, preset=50)
            with Rung(t1.Done):
                copy(temp, saved)
            with Rung(saved >= 85):
                out(output)

        result = prove(logic, ~output, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"Output should fire when Sensor=75 at the scan T1.Done fires, "
            f"got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Output")


class TestFastForwardChainedTimers:
    """Test 10: T2 only starts after T1 finishes.  Verifier must settle
    through both timers."""

    def test_chained_timer_both_done_reachable(self):
        run = Bool("Run", external=True)
        t1 = Timer.clone("Chain1")
        t2 = Timer.clone("Chain2")
        both = Bool("Both")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t1, preset=100)
            with Rung(t1.Done):
                on_delay(t2, preset=100)
            with Rung(t1.Done, t2.Done):
                out(both)

        result = prove(logic, ~both, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"Both should fire after T1 and T2 settle, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Both")

    def test_chained_timer_concrete_agrees(self):
        run = Bool("Run", external=True)
        t1 = Timer.clone("Chain1V")
        t2 = Timer.clone("Chain2V")
        both = Bool("Both")

        with Program(strict=False) as logic:
            with Rung(run):
                on_delay(t1, preset=100)
            with Rung(t1.Done):
                on_delay(t2, preset=100)
            with Rung(t1.Done, t2.Done):
                out(both)

        plc = PLC(logic, dt=1.0)
        plc.force("Run", True)
        for _ in range(250):
            plc.step()
        assert plc.current_state.tags["Both"] is True


# ===================================================================
# Elision
#
# Removes scan-local tags from tracked state.  Risk: misclassifying
# a cross-scan tag as scan-local.
# ===================================================================


class TestElisionNonBool:
    """Gap 4: Concrete elision must cover non-Bool domain paths."""

    def test_mode_gated_cross_scan_value_matters(self):
        """Temp's cross-scan value is observable when Mode=7 reads before write."""
        mode = Int("Mode", external=True, choices={0: "off", 7: "active"})
        sensor = Int("Sensor", external=True, min=0, max=10)
        temp = Int("Temp")
        result_tag = Int("Result")

        with Program(strict=False) as logic:
            with Rung(mode == 7):
                copy(temp, result_tag)
            with Rung():
                calc(sensor + 10, temp)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Mode": 7, "Sensor": 5})
        plc.step()
        assert plc.current_state.tags["Result"] == 0, (
            "first scan: Result should copy Temp's default (0)"
        )
        plc.patch({"Mode": 7, "Sensor": 5})
        plc.step()
        assert plc.current_state.tags["Result"] == 15, (
            "second scan: Result should copy previous scan's Temp (15)"
        )

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)
        stateful = result[0]
        assert "Temp" in stateful, "Temp must be stateful — read before written under Mode=7"

    def test_conditional_read_different_guard_values(self):
        """Tag read-before-write under one guard, write-before-read under another."""
        step = Int("Step", external=True, choices={1: "init", 2: "run", 3: "done"})
        acc = Int("Acc")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(step == 2):
                calc(acc + 1, acc)
            with Rung(step == 3):
                copy(0, acc)
            with Rung(acc >= 5):
                latch(output)

        result = prove(logic, ~output, depth_budget=20)
        assert isinstance(result, Counterexample), (
            "Acc >= 5 is reachable after repeated Step=2 scans"
        )
        _assert_trace_replays(logic, result, "Output")


class TestElisionOneshotInteraction:
    """Test 8: Elision must not remove X, because X's cross-scan value
    matters for Result.  The oneshot fires X for exactly one scan."""

    def test_oneshot_out_fires_result(self):
        condition = Bool("Condition", external=True)
        x = Bool("X")
        result_tag = Bool("Result")

        with Program(strict=False) as logic:
            with Rung(condition):
                out(x, oneshot=True)
            with Rung(x):
                out(result_tag)

        result = prove(logic, ~result_tag, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"Result should fire on the rising edge of Condition via oneshot, "
            f"got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Result")

    def test_oneshot_concrete_single_scan_pulse(self):
        condition = Bool("Condition", external=True)
        x = Bool("X")
        result_tag = Bool("Result")

        with Program(strict=False) as logic:
            with Rung(condition):
                out(x, oneshot=True)
            with Rung(x):
                out(result_tag)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Condition": True})
        plc.step()
        assert plc.current_state.tags["X"] is True
        assert plc.current_state.tags["Result"] is True
        plc.patch({"Condition": True})
        plc.step()
        assert plc.current_state.tags["X"] is False


# ===================================================================
# Integer / Type Overflow
#
# Int wraps at 32767, Dint at 2^31-1.  Both the interpreted engine
# and compiled kernel must agree on overflow semantics.
# ===================================================================


class TestIntegerOverflowWraparound:
    """Gap 3: calc-based int progress must respect PLC integer wrapping."""

    def test_int_wraparound_negative_reachable(self):
        """calc(C + 100, C) on Int — C < 0 must be reachable via wraparound."""
        enable = Bool("Enable", external=True)
        c = Int("Counter")
        fault = Bool("Fault")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(c + 100, c)
            with Rung(c < 0):
                latch(fault)

        plc = PLC(logic, dt=0.010)
        plc.force("Enable", True)
        for _ in range(400):
            plc.step()
        assert plc.current_state.tags["Fault"] is True, (
            "concrete PLC should reach negative via Int wraparound"
        )

        result = prove(logic, ~fault, depth_budget=500)
        assert isinstance(result, Counterexample), (
            f"C < 0 via Int wraparound must be reachable, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Fault")

    def test_dint_large_stride_wraparound(self):
        """calc(C + 1_000_000, C) on Dint — verify wraparound is findable."""
        from pyrung.core import Dint

        enable = Bool("Enable", external=True)
        c = Dint("BigCounter")
        wrapped = Bool("Wrapped")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(c + 1_000_000, c)
            with Rung(c < 0):
                latch(wrapped)

        plc = PLC(logic, dt=0.010)
        plc.force("Enable", True)
        for _ in range(2200):
            plc.step()
        concrete_reached = plc.current_state.tags["Wrapped"]
        if not concrete_reached:
            pytest.skip("Dint wraparound needs more scans than test budget")

        result = prove(logic, ~wrapped, depth_budget=500)
        assert isinstance(result, Counterexample), (
            f"Dint wraparound must be reachable, got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Wrapped")


class TestCompiledKernelOverflowParity:
    """T-6: Does the compiled kernel wrap at 32767 for Int, or use
    Python's unbounded arithmetic?"""

    def test_int_overflow_interpreted_vs_compiled(self):
        from pyrung.circuitpy.codegen import compile_kernel
        from pyrung.core.analysis.prove.kernel import _step_compiled_kernel

        c = Int("C", default=32760)

        with Program(strict=False) as logic:
            with Rung():
                calc(c + 10, c)

        plc = PLC(logic, dt=0.010)
        plc.step()
        interpreted_c = plc.current_state.tags["C"]

        compiled = compile_kernel(logic, blockless=True)
        kernel = compiled.create_kernel()
        kernel.tags["C"] = 32760
        _step_compiled_kernel(compiled, kernel, dt=0.010)
        compiled_c = kernel.tags["C"]

        assert interpreted_c == compiled_c, (
            f"Overflow parity mismatch: interpreted={interpreted_c}, compiled={compiled_c}"
        )


# ===================================================================
# Real / Float State Keys
#
# Real tags with feedback loops create new unique states every scan.
# BFS must either absorb them or declare Intractable.
# ===================================================================


class TestRealFloatStateKeys:
    """Gap 6: Real (float) tags with feedback loops must not cause
    non-termination."""

    def test_real_feedback_loop_is_intractable(self):
        """calc(X + 0.1, X) with Real tag — unbounded, should be Intractable."""
        from pyrung.core import Real

        enable = Bool("Enable", external=True)
        x = Real("X")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(x + 0.1, x)
            with Rung(x > 10.0):
                out(alarm)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            pass
        else:
            result2 = prove(logic, ~alarm, depth_budget=20, max_states=10_000)
            assert not isinstance(result2, Proven) or result2.caveats, (
                "Real feedback loop should either be Intractable or carry a caveat"
            )

    def test_real_comparison_only_absorbs(self):
        """Real tag used only in comparisons — should absorb to threshold vector."""
        from pyrung.core import Real

        enable = Bool("Enable", external=True)
        temp = Real("Temp")
        high = Bool("High")
        low = Bool("Low")

        with Program(strict=False) as logic:
            with Rung(enable):
                calc(temp + 0.5, temp)
            with Rung(temp > 50.0):
                out(high)
            with Rung(temp < 10.0):
                out(low)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            pass
        else:
            states = reachable_states(logic, project=["High", "Low"], depth_budget=10)
            assert not isinstance(states, Intractable), (
                "Real with threshold-only comparisons should be tractable"
            )


# ===================================================================
# Edge Compression
#
# Removes redundant tags from state keys.  Risk: merging states that
# should be distinct.
# ===================================================================


class TestEdgeCompressionMultiWrite:
    """Gap 7: Multiple rungs writing the same output (priority logic)
    must not be collapsed by edge compression."""

    def test_two_latching_writers_same_tag(self):
        """Two rungs latch the same tag from different conditions."""
        mode = Bool("Mode", external=True)
        trigger_a = Bool("TriggerA", external=True)
        trigger_b = Bool("TriggerB", external=True)
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung(mode, rise(trigger_a)):
                latch(flag)
            with Rung(~mode, rise(trigger_b)):
                latch(flag)

        states = reachable_states(logic, project=["Flag", "Mode"], depth_budget=10)
        assert not isinstance(states, Intractable)
        assert frozenset({("Flag", True), ("Mode", True)}) in states, (
            "Flag latched via Mode=True + TriggerA path should be reachable"
        )
        assert frozenset({("Flag", True), ("Mode", False)}) in states, (
            "Flag latched via Mode=False + TriggerB path should be reachable"
        )

    def test_rise_fall_on_same_input_both_edges_explored(self):
        """rise(A) sets Latch, fall(A) sets another — both edges must be explored."""
        from pyrung.core import fall as fall_fn

        a = Bool("A", external=True)
        saw_rise = Bool("SawRise")
        saw_fall = Bool("SawFall")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(saw_rise)
            with Rung(fall_fn(a)):
                latch(saw_fall)

        states = reachable_states(logic, project=["SawRise", "SawFall"], depth_budget=10)
        assert not isinstance(states, Intractable)
        assert frozenset({("SawRise", True), ("SawFall", True)}) in states, (
            "both rise and fall edges must be reachable"
        )


# ===================================================================
# Edge Inputs × Elision (coverage cell (a))
#
# A tag written only on a rising edge is not written every scan, so
# elision must not classify it as scan-local.
# ===================================================================


class TestEdgeInputElision:
    """Edge inputs × elision: rise()/fall() depends on _prev: memory which
    is tracked in the state key via edge compression, independent of whether
    the tag itself is elided as scan-local."""

    def test_edge_written_tag_not_elidable(self):
        """Tag written only on rise() — must be retained as stateful."""
        trigger = Bool("Trigger", external=True)
        flag = Bool("Flag")
        output = Bool("Output")

        with Program(strict=False) as logic:
            with Rung(rise(trigger)):
                latch(flag)
            with Rung(flag):
                out(output)

        result = _classify_dimensions(logic)
        assert not isinstance(result, Intractable)

        result2 = prove(logic, ~output, depth_budget=10)
        assert isinstance(result2, Counterexample), (
            f"Output should fire on rising edge of Trigger, got {type(result2).__name__}"
        )
        _assert_trace_replays(logic, result2, "Output")


# ===================================================================
# Exclusive Inputs
#
# Prunes mutually-exclusive boolean combinations.  Risk: over-pruning
# across scans instead of within a scan.
# ===================================================================


class TestExclusiveInputsCrossScan:
    """Test 7: A, B, C are declared exclusive (at most one True per
    scan).  The violation requires A in one scan and C in a later
    scan — both legal."""

    def test_cross_scan_exclusive_finds_counterexample(self):
        a = Bool("A", external=True)
        Bool("B", external=True)
        c = Bool("C", external=True)
        latched = Bool("Latched")
        bad = Bool("Bad")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(latched)
            with Rung(latched, c):
                out(bad)

        result = prove(
            logic,
            ~bad,
            exclusive_inputs=(("A", "B", "C"),),
            depth_budget=10,
        )
        assert isinstance(result, Counterexample), (
            f"Bad should be reachable: A rises in scan 1, C true in scan 2, "
            f"got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Bad")

    def test_cross_scan_exclusive_concrete_agrees(self):
        a = Bool("A", external=True)
        Bool("B", external=True)
        c = Bool("C", external=True)
        latched = Bool("Latched")
        bad = Bool("Bad")

        with Program(strict=False) as logic:
            with Rung(rise(a)):
                latch(latched)
            with Rung(latched, c):
                out(bad)

        plc = PLC(logic, dt=0.010)
        plc.patch({"A": True})
        plc.step()
        assert plc.current_state.tags["Latched"] is True
        plc.patch({"A": False, "C": True})
        plc.step()
        assert plc.current_state.tags["Bad"] is True


# ===================================================================
# receive()
#
# Modbus receive destinations are nondeterministic.  Risk: domain not
# seeded from comparison boundaries.
# ===================================================================


class TestReceiveDomainCompleteness:
    """Test 9: Without a declared domain, does the verifier explore
    enough values to hit the comparison boundary?"""

    def test_receive_dest_hits_comparison_boundary(self):
        from pyrung.core.instruction.send_receive import ModbusTcpTarget, receive

        enable = Bool("Enable", external=True)
        value = Int("Value", min=0, max=1000)
        receiving = Bool("Receiving")
        success = Bool("Success")
        error = Bool("Error")
        ex_code = Int("ExCode")
        high = Bool("High")

        target = ModbusTcpTarget("peer", "127.0.0.1", port=502, device_id=1)

        with Program(strict=False) as logic:
            with Rung(enable):
                receive(
                    target=target,
                    remote_start="DS1",
                    dest=value,
                    receiving=receiving,
                    success=success,
                    error=error,
                    exception_response=ex_code,
                )
            with Rung(value >= 500):
                out(high)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            pytest.skip(f"receive() domain intractable: {result.reason}")

        result2 = prove(logic, ~high, depth_budget=10)
        assert isinstance(result2, Counterexample), (
            f"High should fire when Value receives >=500, got {type(result2).__name__}"
        )
        _assert_trace_replays(logic, result2, "High")


# ===================================================================
# OTE inside ForLoop with dynamic count
#
# When an out() is nested inside a ForLoop whose count is a tag (can
# be 0 at runtime), the OTE may not execute every scan.  That makes
# the tag stateful, not combinational.  Risk: misclassified as
# combinational, states merged, reachable states missed.
# ===================================================================


class TestOteInForLoopClassification:
    """Test 10: OTE inside dynamic-count ForLoop must not be
    classified as combinational."""

    def test_ote_in_dynamic_forloop_not_combinational(self):
        """An out() inside a ForLoop(count_tag) where count can be 0
        must be classified as stateful, not combinational."""
        from pyrung.core import ForLoop

        enable = Bool("Enable", external=True)
        count_tag = Int("Count", external=True, min=0, max=2)
        light = Bool("Light")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                with ForLoop(count_tag):
                    out(light)
            with Rung(light):
                out(alarm)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            pytest.skip(f"intractable: {result.reason}")

        stateful, nondeterministic, _comb, *_ = result
        assert "Light" in stateful or "Light" in nondeterministic, (
            "Light should be stateful (or ND), not combinational — "
            "ForLoop count can be 0, so OTE may not execute"
        )

    def test_ote_in_dynamic_forloop_prove_finds_counterexample(self):
        """prove() must find that Alarm is reachable via ForLoop count=0
        retention: Enable=True + Count>=1 sets Light, then on the same
        or later scan Active=False fires Alarm."""
        from pyrung.core import ForLoop

        enable = Bool("Enable", external=True)
        count_tag = Int("Count", external=True, min=0, max=2)
        active = Bool("Active", external=True)
        light = Bool("Light")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                with ForLoop(count_tag):
                    out(light)
            with Rung(light, ~active):
                out(alarm)

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            f"Alarm should be reachable: Enable=True + Count>=1 sets Light, "
            f"Active=False fires Alarm. Got {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")

    def test_ote_in_dynamic_forloop_concrete_agrees(self):
        """Concrete PLC confirms: Light stays True when count drops to 0."""
        from pyrung.core import ForLoop

        enable = Bool("Enable", external=True)
        count_tag = Int("Count", external=True, min=0, max=2)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(enable):
                with ForLoop(count_tag):
                    out(light)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True, "Count": 1})
        plc.step()
        assert plc.current_state.tags["Light"] is True

        plc.patch({"Enable": True, "Count": 0})
        plc.step()
        assert plc.current_state.tags["Light"] is True, (
            "Light should retain True — ForLoop body skipped when Count=0"
        )

    def test_ote_in_static_forloop_remains_combinational(self):
        """A ForLoop with a positive literal count always executes,
        so the OTE is still combinational."""
        from pyrung.core import ForLoop

        enable = Bool("Enable", external=True)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(enable):
                with ForLoop(3):
                    out(light)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            pytest.skip(f"intractable: {result.reason}")

        stateful, nondeterministic, _comb, *_ = result
        assert "Light" not in stateful and "Light" not in nondeterministic, (
            "Light should be combinational — ForLoop(3) always executes"
        )


# ===================================================================
# Classifier boundary back-propagation gaps
#
# The classifier currently propagates downstream comparison boundaries
# back through copy() and calc(source +/- k), but not through more
# general calc() shapes or structural writers like fill() and
# blockcopy(). That can shrink ND domains below the values required to
# reach a downstream comparison.
# ===================================================================


class TestClassifierBackPropagationGaps:
    """Test 11: unsupported reverse edges must not under-approximate ND domains."""

    def test_calc_multiplication_backprop_required_for_nd_input(self):
        level = Int("Level", external=True)
        stored = Int("Stored")
        alarm_a = Bool("AlarmA")
        alarm_b = Bool("AlarmB")

        with Program(strict=False) as logic:
            with Rung(level > 100):
                latch(alarm_a)
            with Rung():
                calc(level * 2, stored)
            with Rung(stored == 150):
                latch(alarm_b)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Level": 75})
        plc.step()
        assert plc.current_state.tags["Stored"] == 150
        assert plc.current_state.tags["AlarmB"] is True

        result = prove(logic, ~alarm_b, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Level=75 should flow through calc(level * 2) to Stored=150, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "AlarmB")

    def test_fill_from_tag_backprop_required_for_nd_input(self):
        src = Int("Src", external=True)
        dst = Block("Dst", TagType.INT, 1, 1)
        alarm_a = Bool("AlarmA")
        alarm_b = Bool("AlarmB")

        with Program(strict=False) as logic:
            with Rung(src > 100):
                latch(alarm_a)
            with Rung():
                fill(src, dst.select(1, 1))
            with Rung(dst[1] == 75):
                latch(alarm_b)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Src": 75})
        plc.step()
        assert plc.current_state.tags["Dst1"] == 75
        assert plc.current_state.tags["AlarmB"] is True

        result = prove(logic, ~alarm_b, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Src=75 should flow through fill(src, Dst[1]) to Dst1=75, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "AlarmB")

    def test_blockcopy_backprop_required_for_nd_input(self):
        src = Block("Src", TagType.INT, 1, 1)
        src.slot(1, external=True)
        dst = Block("Dst", TagType.INT, 1, 1)
        alarm_a = Bool("AlarmA")
        alarm_b = Bool("AlarmB")

        with Program(strict=False) as logic:
            with Rung(src[1] > 100):
                latch(alarm_a)
            with Rung():
                blockcopy(src.select(1, 1), dst.select(1, 1))
            with Rung(dst[1] == 75):
                latch(alarm_b)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Src1": 75})
        plc.step()
        assert plc.current_state.tags["Dst1"] == 75
        assert plc.current_state.tags["AlarmB"] is True

        result = prove(logic, ~alarm_b, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Src1=75 should flow through blockcopy(Src1, Dst1) to Dst1=75, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "AlarmB")

    def test_transitive_copy_then_calc_multiplication(self):
        """copy(Level, Stored) then calc(Stored * 2, Shifted) with Shifted == 150
        must propagate 75 back through both hops to Level."""
        level = Int("Level", external=True)
        stored = Int("Stored")
        shifted = Int("Shifted")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(level > 100):
                latch(alarm)
            with Rung():
                copy(level, stored)
            with Rung():
                calc(stored * 2, shifted)
            with Rung(shifted == 150):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Level": 75})
        plc.step()
        assert plc.current_state.tags["Shifted"] == 150
        assert plc.current_state.tags["Alarm"] is True

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Level=75 should flow through copy+calc(*2) to Shifted=150, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")


# ===================================================================
# Pointer-indirect backward propagation
#
# copy(source, block[pointer]) and similar indirect writes must
# propagate comparison boundaries from the block's concrete elements
# back to the source, using the pointer's min/max to bound the
# expansion.
# ===================================================================


class TestIndirectBackPropagation:
    """Backward propagation through pointer-indirect writes."""

    def test_copy_to_indirect_ref_backprop(self):
        """copy(source, block[ptr]) with block[1] == 42 must seed Source=42."""
        block = Block("B", TagType.INT, 1, 5)
        pointer = Int("Ptr", external=True, min=1, max=5)
        source = Int("Source", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                copy(source, block[pointer])
            with Rung(block[1] == 42):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Source": 42, "Ptr": 1})
        plc.step()
        assert plc.current_state.tags["B1"] == 42
        assert plc.current_state.tags["Alarm"] is True

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Source=42 should flow through copy(source, B[Ptr]) to B1=42, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")

    def test_fill_to_indirect_range_backprop(self):
        """fill(source, block.select(ptr, ptr)) with block[1] == 75 must seed Source=75."""
        block = Block("B", TagType.INT, 1, 3)
        pointer = Int("Ptr", external=True, min=1, max=3)
        source = Int("Source", external=True)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                fill(source, block.select(pointer, pointer))
            with Rung(block[1] == 75):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Source": 75, "Ptr": 1})
        plc.step()
        assert plc.current_state.tags["B1"] == 75
        assert plc.current_state.tags["Alarm"] is True

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Source=75 should flow through fill(source, B[Ptr..Ptr]) to B1=75, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")

    def test_blockcopy_to_indirect_range_backprop(self):
        """blockcopy(static, block.select(ptr, ptr)) with block[1] == 75 must seed Src1=75."""
        src = Block("Src", TagType.INT, 1, 1)
        src.slot(1, external=True)
        dst = Block("Dst", TagType.INT, 1, 3)
        pointer = Int("Ptr", external=True, min=1, max=3)
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                blockcopy(src.select(1, 1), dst.select(pointer, pointer))
            with Rung(dst[1] == 75):
                latch(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Src1": 75, "Ptr": 1})
        plc.step()
        assert plc.current_state.tags["Dst1"] == 75
        assert plc.current_state.tags["Alarm"] is True

        result = prove(logic, ~alarm, depth_budget=10)
        assert isinstance(result, Counterexample), (
            "Src1=75 should flow through blockcopy to Dst1=75, "
            f"but prove returned {type(result).__name__}"
        )
        _assert_trace_replays(logic, result, "Alarm")


class TestExplanationSoundness:
    def test_explain_batch_partition_sharing(self):
        a = Bool("A", external=True)
        b = Bool("B", external=True)
        c = Bool("C", external=True)
        x = Bool("X")
        y = Bool("Y")
        z = Bool("Z")
        with Program() as logic:
            with Rung(a):
                out(x)
            with Rung(b):
                out(y)
            with Rung(c):
                out(z)

        prop_x = (Or(x, ~a),)
        prop_y = (Or(y, ~b),)
        prop_z = (Or(z, ~c),)

        results = prove(logic, [prop_x, prop_y, prop_z], explain=True)
        assert isinstance(results, list)
        assert len(results) == 3
        for r in results:
            assert isinstance(r, Proven)
            assert r.explanation is not None

    def test_explain_with_threshold_absorption(self):
        inp = Bool("Inp", external=True)
        t = Timer.clone("T")
        alarm = Bool("Alarm")
        with Program() as logic:
            with Rung(inp):
                on_delay(t, 100)
            with Rung(t.Done):
                out(alarm)

        result = prove(logic, Or(~alarm, t.Done), explain=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        all_kinds = set()
        for entry in expl:
            for d in entry.decisions:
                all_kinds.add(d.kind)
        assert "classification" in all_kinds

    def test_explain_skip_optimizations_pass_disabled(self):
        inp = Bool("Inp", external=True)
        out_tag = Bool("Out")
        with Program() as logic:
            with Rung(inp):
                out(out_tag)

        result = prove(logic, Or(out_tag, ~inp), explain=True, _skip_optimizations=True)
        assert isinstance(result, Proven)
        expl = result.explanation
        assert expl is not None
        disabled_notes = [n for n in expl.notes if "disabled" in n]
        assert len(disabled_notes) >= 3
