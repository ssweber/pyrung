"""Plain runs of the program shapes the deleted search's matrix covered.

Each section names a fact the shape stresses.  A test steps the program on the
PLC (and on the compiled kernel where they could differ) and checks what it
stores.  A few check how the facts classify the shape.
"""

from __future__ import annotations

import pytest

from pyrung.core import (
    PLC,
    Bool,
    Int,
    Program,
    Rung,
    Timer,
    copy,
    latch,
    math,
    on_delay,
    out,
    rise,
)
from pyrung.core.analysis.prove import Intractable
from pyrung.core.analysis.prove.classify import _classify_dimensions

# ===================================================================
# Threshold Absorption
#
# Jumps counters/timers directly to comparison boundaries instead of
# stepping.  Risks: missing boundaries, assuming monotonicity when
# conditional resets exist.
# ===================================================================


class TestThresholdAbsorptionIndirectCopy:
    """Test 1: Comparison against a copy of the timer accumulator, not
    the accumulator directly.  The facts must propagate the threshold
    backward through the copy instruction."""

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


class TestBackwardPropagationNonInvertible:
    """Test 2: Sensor * Sensor can't be uniquely inverted.  The facts
    must still find an input that satisfies the comparison."""

    def test_squared_concrete_agrees(self):
        sensor = Int("Sensor", external=True, min=0, max=20)
        squared = Int("Squared")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung():
                math(sensor * sensor, squared)
            with Rung(squared >= 100):
                out(alarm)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Sensor": 10})
        plc.step()
        assert plc.current_state.tags["Alarm"] is True


class TestBackwardPropagationMultiHop:
    """Test 3: Boundary value 85 must propagate back through +10 then
    through copy to seed Level's domain with 75."""

    def test_two_hop_concrete_agrees(self):
        level = Int("Level", external=True, min=0, max=100)
        stored = Int("Stored")
        shifted = Int("Shifted")
        high = Bool("High")

        with Program(strict=False) as logic:
            with Rung():
                copy(level, stored)
            with Rung():
                math(stored + 10, shifted)
            with Rung(shifted >= 85):
                out(high)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Level": 75})
        plc.step()
        assert plc.current_state.tags["High"] is True


class TestBackwardPropagationElisionPromotion:
    """Test 11: When elision promotes a tag from auto-elided to stateful,
    backward propagation must seed its domain."""

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


class TestBackwardPropagationWordBitwise:
    """T-2: Backward propagation can invert addition.  Can it invert
    a bitwise AND?"""

    def test_bitwise_and_concrete_agrees(self):
        from pyrung.core import Word

        mask = Word("Mask", external=True, min=0, max=255)
        masked = Word("Masked")
        flag = Bool("Flag")

        with Program(strict=False) as logic:
            with Rung():
                math(mask & 0x00FF, masked)
            with Rung(masked >= 16):
                out(flag)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Mask": 32})
        plc.step()
        assert plc.current_state.tags["Flag"] is True


class TestBackwardPropagationIntEquality:
    """T-4: An external Int with an equality check.  Does backward
    propagation seed the exact value?"""

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


class TestFastForwardChainedTimers:
    """Test 10: T2 only starts after T1 finishes.  Verifier must settle
    through both timers."""

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
                math(sensor + 10, temp)

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


class TestElisionOneshotInteraction:
    """Test 8: Elision must not remove X, because X's cross-scan value
    matters for Result.  The oneshot fires X for exactly one scan."""

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


class TestCompiledKernelOverflowParity:
    """T-6: Does the compiled kernel wrap at 32767 for Int, or use
    Python's unbounded arithmetic?"""

    def test_int_overflow_interpreted_vs_compiled(self):
        from pyrung.circuitpy.codegen import compile_kernel
        from tests.core.kernel_step import step_compiled_kernel

        c = Int("C", default=32760)

        with Program(strict=False) as logic:
            with Rung():
                math(c + 10, c)

        plc = PLC(logic, dt=0.010)
        plc.step()
        interpreted_c = plc.current_state.tags["C"]

        compiled = compile_kernel(logic, blockless=True)
        kernel = compiled.create_kernel()
        kernel.tags["C"] = 32760
        step_compiled_kernel(compiled, kernel, dt=0.010)
        compiled_c = kernel.tags["C"]

        assert interpreted_c == compiled_c, (
            f"Overflow parity mismatch: interpreted={interpreted_c}, compiled={compiled_c}"
        )


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
# OTE inside ForLoop
#
# As on CLICK, a false loop rung skips the body, so an out() there holds
# its value.  A count tag that resolves to 0 runs no iteration, so the
# coil holds then too.  Such a coil is stateful, not combinational.
# ===================================================================


class TestOteInForLoopClassification:
    """Test 10: an OTE inside a ForLoop holds while the loop does not run."""

    def test_ote_in_dynamic_forloop_is_stateful(self):
        """An out() inside ForLoop(count_tag) holds when the count is 0."""
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

        stateful, _nondeterministic, _comb, *_ = result
        assert "Light" in stateful, (
            "Light holds across scans: a false loop rung or a count of 0 skips its out()"
        )

    def test_ote_in_dynamic_forloop_concrete_agrees(self):
        """Concrete PLC confirms: count 0 runs no iteration, a false rung skips the body."""
        from pyrung.core import ForLoop

        enable = Bool("Enable", external=True)
        count_tag = Int("Count", external=True, min=0, max=2)
        light = Bool("Light")

        with Program(strict=False) as logic:
            with Rung(enable):
                with ForLoop(count_tag):
                    out(light)

        plc = PLC(logic, dt=0.010)
        plc.patch({"Enable": True, "Count": 0})
        plc.step()
        assert plc.current_state.tags["Light"] is False

        plc.patch({"Count": 1})
        plc.step()
        assert plc.current_state.tags["Light"] is True

        plc.patch({"Enable": False})
        plc.step()
        assert plc.current_state.tags["Light"] is True

    def test_ote_in_static_forloop_is_stateful_behind_a_condition(self):
        """A ForLoop(3) behind a contact skips its out() when the contact opens."""
        from pyrung.core import ForLoop

        enable = Bool("Enable", external=True)
        light = Bool("Light")
        alarm = Bool("Alarm")

        with Program(strict=False) as logic:
            with Rung(enable):
                with ForLoop(3):
                    out(light)
            with Rung(light):
                out(alarm)

        result = _classify_dimensions(logic)
        if isinstance(result, Intractable):
            pytest.skip(f"intractable: {result.reason}")

        stateful, _nondeterministic, _comb, *_ = result
        assert "Light" in stateful, "Light holds while Enable is false"
