"""Tests for Counter instructions (CTU, CTD).

Counters are terminal instructions that count the rising edges of their count
inputs, as a CLICK does: each counter keeps its own memory of each input,
updated on every scan it runs (reset scans included).  They manipulate both a
done bit and an accumulator.
"""

import pytest

from pyrung.core import (
    Bool,
    Counter,
    Dint,
    Int,
    Program,
    Real,
    Rung,
    copy,
    count_down,
    count_up,
    latch,
    out,
    rise,
    udt,
)


def _pulse(runner, name: str, times: int = 1) -> None:
    """Turn *name* on for one scan and off for one, *times* times."""
    for _ in range(times):
        runner.patch({name: True})
        runner.step()
        runner.patch({name: False})
        runner.step()


class TestCountUpInstruction:
    """Test Count Up (CTU) instruction."""

    def test_count_up_counts_rising_edges(self, runner_factory):
        """CTU counts the rising edges of its rung, not the scans it is true.

        Click behavior: a held input counts once; it must go false and true
        again to count again.
        """
        PartSensor = Bool("PartSensor")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(PartSensor):
                count_up(Counter[1], preset=5).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"PartSensor": False, "ResetBtn": False})
        runner.step()

        # The rising edge counts once
        runner.patch({"PartSensor": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1

        runner.step()  # Still true - no new edge
        assert runner.current_state.tags["Counter_Acc"] == 1
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1

        # False, then true again - a second edge
        runner.patch({"PartSensor": False})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1
        runner.patch({"PartSensor": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2

    def test_count_up_sets_done_bit_at_preset(self, runner_factory):
        """CTU done bit turns ON when accumulator >= preset."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=3).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Count up to preset - 3 pulses
        _pulse(runner, "Trigger", 3)

        # Should be at preset now
        assert runner.current_state.tags["Counter_Acc"] == 3
        assert runner.current_state.tags["Counter_Done"] is True  # Done bit ON

        # Continue counting - done bit stays ON
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == 4
        assert runner.current_state.tags["Counter_Done"] is True

    def test_count_up_reset(self, runner_factory):
        """CTU reset clears both done bit and accumulator."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=5).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Count up some - 2 pulses
        _pulse(runner, "Trigger", 2)

        assert runner.current_state.tags["Counter_Acc"] == 2

        # Activate reset
        runner.patch({"ResetBtn": True})
        runner.step()

        assert runner.current_state.tags["Counter_Acc"] == 0
        assert runner.current_state.tags["Counter_Done"] is False

    def test_held_input_not_recounted_after_reset(self, runner_factory):
        """The edge memory updates on reset scans: a held input counts once."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=5).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": True, "ResetBtn": False})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1

        # Reset with the input still on, then release the reset
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0
        runner.patch({"ResetBtn": False})
        runner.step()
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0

        # An edge that arrives during the reset is swallowed too
        runner.patch({"Trigger": False})
        runner.step()
        runner.patch({"Trigger": True, "ResetBtn": True})
        runner.step()
        runner.patch({"ResetBtn": False})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0

    def test_input_on_at_first_scan_counts(self, runner_factory):
        """The edge memory starts off: an input on from the first scan counts."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=1).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": True, "ResetBtn": False})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1
        assert runner.current_state.tags["Counter_Done"] is True

    def test_edge_memory_is_site_memory(self, runner_factory):
        """The counter's input memory lives under its site key in state memory."""
        Trigger = Bool("Trigger")
        Down = Bool("Down")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=5).down(Down).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": True, "Down": False, "ResetBtn": False})
        runner.step()
        memory = runner.current_state.memory
        cu = [k for k in memory if k.startswith("_cu:")]
        cd = [k for k in memory if k.startswith("_cd:")]
        assert len(cu) == 1 and len(cd) == 1
        assert memory[cu[0]] is True
        assert memory[cd[0]] is False

    def test_count_up_with_down_bidirectional(self, runner_factory):
        """CTU with .down() creates bidirectional counter."""
        Enter = Bool("Enter")
        Exit = Bool("Exit")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(rise(Enter)):
                count_up(Counter[1], preset=10).down(rise(Exit)).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Enter": False, "Exit": False, "ResetBtn": False})
        runner.step()

        # Increment on Enter rising edge
        runner.patch({"Enter": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1

        runner.patch({"Enter": False})
        runner.step()

        runner.patch({"Enter": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2

        # Decrement on Exit rising edge
        runner.patch({"Enter": False, "Exit": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1

        runner.patch({"Exit": False})
        runner.step()

        runner.patch({"Exit": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0

    def test_count_up_reset_condition_uses_rung_entry_snapshot(self, runner_factory):
        """Same-rung writes do not fire reset() until the next snapshot."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                latch(ResetBtn)
                count_up(Counter[1], preset=5).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "ResetBtn": False})

        runner.step()
        assert runner.current_state.tags["ResetBtn"] is True
        assert runner.current_state.tags["Counter_Acc"] == 1

        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0
        assert runner.current_state.tags["Counter_Done"] is False

    def test_count_up_down_condition_uses_rung_entry_snapshot(self, runner_factory):
        """Same-rung writes do not affect bidirectional down() until next scan."""
        Enable = Bool("Enable")
        Down = Bool("Down")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                latch(Down)
                count_up(Counter[1], preset=5).down(Down).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Enable": True, "Down": False, "ResetBtn": False})

        runner.step()
        assert runner.current_state.tags["Down"] is True
        assert runner.current_state.tags["Counter_Acc"] == 1


class TestCountDownInstruction:
    """Test Count Down (CTD) instruction."""

    def test_count_down_counts_rising_edges(self, runner_factory):
        """CTD counts the rising edges of its rung down, not the scans it is true.

        Click behavior: starts at 0, counts down to negative values.
        """
        Dispense = Bool("Dispense")
        Reload = Bool("Reload")

        with Program() as logic:
            with Rung(Dispense):
                count_down(Counter[1], preset=5).reset(Reload)

        runner = runner_factory(logic)
        runner.patch({"Dispense": False, "Reload": False})
        runner.step()

        # Initialize with reset - should clear to 0
        runner.patch({"Reload": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0

        runner.patch({"Reload": False})
        runner.step()

        # The rising edge counts once; holding it counts nothing more
        runner.patch({"Dispense": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == -1
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == -1

        # Two more edges
        runner.patch({"Dispense": False})
        runner.step()
        _pulse(runner, "Dispense", 2)
        assert runner.current_state.tags["Counter_Acc"] == -3

    def test_count_down_done_at_preset(self, runner_factory):
        """CTD done bit is ON while accumulator <= preset.

        Click behavior: a down counter starts at 0, so with a positive preset
        it is done from its first scan; staged above the preset, it is done
        once it counts down to it.
        """
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter[1], preset=3).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()
        # 0 <= 3: done from the first scan
        assert runner.current_state.tags["Counter_Acc"] == 0
        assert runner.current_state.tags["Counter_Done"] is True

        # Reset clears Done for its scan
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0
        assert runner.current_state.tags["Counter_Done"] is False
        runner.patch({"ResetBtn": False})
        runner.step()
        assert runner.current_state.tags["Counter_Done"] is True

        # Staged at 5, two edges take it to 3 (done)
        runner.patch({"Counter_Acc": 5})
        runner.step()
        assert runner.current_state.tags["Counter_Done"] is False  # 5 > 3
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == 4
        assert runner.current_state.tags["Counter_Done"] is False
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == 3
        assert runner.current_state.tags["Counter_Done"] is True  # Done bit ON

        # Continue counting - done bit stays ON
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == 2
        assert runner.current_state.tags["Counter_Done"] is True

    def test_count_down_reset_clears_to_zero(self, runner_factory):
        """CTD reset clears accumulator to 0.

        Click behavior: Reset sets acc to 0, not to preset.
        """
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter[1], preset=10).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Count down some: 0 -> -1 -> -2
        runner.patch({"ResetBtn": True})
        runner.step()
        runner.patch({"ResetBtn": False})
        _pulse(runner, "Trigger", 2)

        assert runner.current_state.tags["Counter_Acc"] == -2

        # Activate reset - should clear to 0
        runner.patch({"ResetBtn": True})
        runner.step()

        assert runner.current_state.tags["Counter_Acc"] == 0
        assert runner.current_state.tags["Counter_Done"] is False


class TestCounterAccumulatorClamp:
    """Tests for DINT clamp behavior in counter accumulators."""

    def test_ctu_accumulator_clamps_at_dint_max(self, runner_factory):
        """CTU accumulator saturates at DINT max (2147483647)."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=2147483647).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Prime to one below max, then increment into max
        runner.patch({"Trigger": True, "Counter_Acc": 2147483646})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2147483647
        assert runner.current_state.tags["Counter_Done"] is True

        # Further increments stay clamped
        runner.patch({"Trigger": False})
        runner.step()
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == 2147483647
        assert runner.current_state.tags["Counter_Done"] is True

    def test_ctd_accumulator_clamps_at_dint_min(self, runner_factory):
        """CTD accumulator saturates at DINT min (-2147483648)."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter[1], preset=1).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Prime to one above min, then decrement into min
        runner.patch({"Trigger": True, "Counter_Acc": -2147483647})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == -2147483648
        assert runner.current_state.tags["Counter_Done"] is True

        # Further decrements stay clamped
        runner.patch({"Trigger": False})
        runner.step()
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == -2147483648
        assert runner.current_state.tags["Counter_Done"] is True

    def test_ctu_bidirectional_clamp_applies_after_net_delta(self, runner_factory):
        """Bidirectional CTU clamps after applying the net (+1/-1) scan delta."""
        Enable = Bool("Enable")
        Down = Bool("Down")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                count_up(Counter[1], preset=2147483647).down(Down).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Enable": False, "Down": False, "ResetBtn": False})
        runner.step()

        # Up and down edges on the same scan cancel (stays at max)
        runner.patch({"Enable": True, "Down": True, "Counter_Acc": 2147483647})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2147483647

        # A down edge alone still decrements from the clamped value
        runner.patch({"Enable": False, "Down": False})
        runner.step()
        runner.patch({"Down": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2147483646

    def test_up_and_down_edges_same_scan_cancel(self, runner_factory):
        """An up and a down edge on one scan leave the count where it was."""
        Up = Bool("Up")
        Down = Bool("Down")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Up):
                count_up(Counter[1], preset=2).down(Down).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Up": False, "Down": False, "ResetBtn": False})
        runner.step()
        _pulse(runner, "Up", 2)
        assert runner.current_state.tags["Counter_Acc"] == 2
        runner.patch({"Up": True, "Down": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2
        # Held, neither counts
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2
        # Down falls and rises with Up held: only the down edge counts
        runner.patch({"Down": False})
        runner.step()
        runner.patch({"Down": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1


class TestCounterIntegration:
    """Integration tests for counter instructions."""

    def test_counter_accumulates_mid_scan_visible_to_later_rungs(self, runner_factory):
        """Counter should accumulate during the scan and be visible to later rungs.

        This test verifies that when a counter increments in one rung, subsequent rungs
        in the SAME scan can see the updated accumulator value.

        Based on physical CLICK PLC testing (TEST_SCRATCHPAD.md):
        - Counter increments immediately when its rung executes
        - Later rungs can check the accumulator value and execute based on it
        - This all happens within a single scan cycle
        """

        # Tags
        DataTest = Int("DataTest")
        CopiedCounterBeforeEnd = Int("CopiedCounterBeforeEnd")
        Val2MultiplyInPlace = Int("Val2MultiplyInPlace")

        with Program() as logic:
            # Rung 1: Count up when DataTest == 1, reset when DataTest == 2
            with Rung(DataTest == 1):
                count_up(Counter[1], preset=10).reset(DataTest == 2)

            # Rung 2: When counter acc reaches 1, do operations (mid-scan!)
            # This rung should execute IN THE SAME SCAN as the counter increment
            with Rung(DataTest == 1, Counter[1].Acc == 1):
                copy(Counter[1].Acc, CopiedCounterBeforeEnd)
                copy(10, Val2MultiplyInPlace)
                # User note: In physical test, this was math operations (* 10 twice)
                # We'll just copy values to verify execution

            # Rung 3: Change DataTest to 2 (will reset counter next scan)
            with Rung(DataTest == 1):
                copy(2, DataTest)

        runner = runner_factory(logic)
        runner.patch({"DataTest": 1, "ResetBtn": False})

        # Execute 1 scan
        runner.step()

        # Expected behavior after 1 scan (based on physical CLICK test):
        # - Counter incremented to 1 during the scan
        # - Rung 2 saw TestCounter_acc == 1 and executed
        # - CopiedCounterBeforeEnd captured the counter value (1)
        # - Val2MultiplyInPlace was set to 10
        # - DataTest changed to 2 at end of scan
        assert runner.current_state.tags["Counter_Acc"] == 1, (
            "Counter should have incremented to 1 during first scan"
        )
        assert runner.current_state.tags["CopiedCounterBeforeEnd"] == 1, (
            "Rung 2 should have executed mid-scan and copied counter value"
        )
        assert runner.current_state.tags["Val2MultiplyInPlace"] == 10, (
            "Rung 2 should have executed and set Val2MultiplyInPlace"
        )
        assert runner.current_state.tags["DataTest"] == 2, (
            "DataTest should be 2 at end of first scan"
        )

        # Execute scan 2
        runner.step()

        # Expected behavior after scan 2:
        # - Counter reset because DataTest == 2
        assert runner.current_state.tags["Counter_Acc"] == 0, (
            "Counter should reset to 0 on second scan (DataTest == 2)"
        )
        assert runner.current_state.tags["Counter_Done"] is False, (
            "Counter done bit should be false after reset"
        )

    def test_counter_fires_mid_scan_current_implementation(self, runner_factory):
        """A counter's count is visible to later rungs in the same scan.

        Based on physical CLICK PLC testing.
        """
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        SawCounterAt1 = Bool("SawCounterAt1")
        SawCounterAt2 = Bool("SawCounterAt2")

        with Program() as logic:
            # Rung 1: Count up on each rising edge of Trigger
            with Rung(Trigger):
                count_up(Counter[1], preset=5).reset(ResetBtn)

            # Rung 2: Set flag if we see counter == 1
            with Rung(Counter[1].Acc == 1):
                latch(SawCounterAt1)

            # Rung 3: Set flag if we see counter == 2
            with Rung(Counter[1].Acc == 2):
                latch(SawCounterAt2)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Scan 1: Enable trigger - counter should increment to 1
        runner.patch({"Trigger": True})
        runner.step()

        # Check: Did we see the counter at 1 in the SAME scan?
        assert runner.current_state.tags["Counter_Acc"] == 1, (
            "Counter should be at 1 after first scan"
        )
        assert runner.current_state.tags["SawCounterAt1"] is True, (
            "Should have seen counter == 1 mid-scan (Rung 2 executed same scan as increment)"
        )

        # Trigger off, then on again: the counter should increment to 2
        runner.patch({"Trigger": False})
        runner.step()
        runner.patch({"Trigger": True})
        runner.step()

        # Check: Did we see the counter at 2 in the SAME scan?
        assert runner.current_state.tags["Counter_Acc"] == 2, (
            "Counter should be at 2 after the second edge"
        )
        assert runner.current_state.tags["SawCounterAt2"] is True, (
            "Should have seen counter == 2 mid-scan (Rung 3 executed same scan as increment)"
        )

    def test_counter_in_production_line(self, runner_factory):
        """Test counter used in a production line scenario."""
        PartSensor = Bool("PartSensor")
        BatchComplete = Bool("BatchComplete")
        ResetButton = Bool("ResetButton")
        HalfwayLight = Bool("HalfwayLight")

        with Program() as logic:
            # Count parts
            with Rung(rise(PartSensor)):
                count_up(Counter[1], preset=100).reset(ResetButton)

            # Batch complete output
            with Rung(Counter[1].Done):
                out(BatchComplete)

            # Halfway indicator
            with Rung(Counter[1].Acc >= 50):
                out(HalfwayLight)

        runner = runner_factory(logic)
        runner.patch(
            {
                "PartSensor": False,
                "ResetButton": False,
                "BatchComplete": False,
                "HalfwayLight": False,
            }
        )
        runner.step()

        # Count up to 50
        for _ in range(50):
            runner.patch({"PartSensor": True})
            runner.step()
            runner.patch({"PartSensor": False})
            runner.step()

        assert runner.current_state.tags["Counter_Acc"] == 50
        assert runner.current_state.tags["HalfwayLight"] is True
        assert runner.current_state.tags["BatchComplete"] is False

        # Count up to 100
        for _ in range(50):
            runner.patch({"PartSensor": True})
            runner.step()
            runner.patch({"PartSensor": False})
            runner.step()

        assert runner.current_state.tags["Counter_Acc"] == 100
        assert runner.current_state.tags["Counter_Done"] is True
        assert runner.current_state.tags["BatchComplete"] is True

    def test_counter_in_branch_requires_parent_and_branch_conditions(self, runner_factory):
        """Counter in branch should require BOTH parent rung AND branch conditions."""
        from pyrung.core.program import branch

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Step == 0):
                with branch(AutoMode):
                    count_up(Counter[1], preset=5).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Step": 0, "AutoMode": False, "ResetBtn": False})
        runner.step()

        # Case 1: Parent true, branch rises - increments
        runner.patch({"AutoMode": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1

        runner.patch({"AutoMode": False})
        runner.step()

        # Case 2: Parent false, branch true - should NOT increment
        runner.patch({"Step": 1, "AutoMode": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 1  # Should stay at 1

        # Case 3: Parent goes true with the branch true - the combined power
        # rises, so it SHOULD increment
        runner.patch({"Step": 0})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 2

    def test_count_down_in_branch_requires_parent_and_branch_conditions(self, runner_factory):
        """Count down in branch should require BOTH parent rung AND branch conditions."""
        Enable = Bool("Enable")
        Mode = Bool("Mode")
        ResetBtn = Bool("ResetBtn")

        from pyrung.core.program import branch

        with Program() as logic:
            with Rung(Enable):
                with branch(Mode):
                    count_down(Counter[1], preset=10).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Enable": False, "Mode": False, "ResetBtn": False})
        runner.step()

        # Initialize with reset - clears to 0
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0

        runner.patch({"ResetBtn": False})
        runner.step()

        # Case 1: Parent false, branch true - should NOT decrement
        runner.patch({"Enable": False, "Mode": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0  # Should stay at 0

        # Case 2: Parent true, branch false - should NOT decrement
        runner.patch({"Enable": True, "Mode": False})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 0  # Should stay at 0

        # Case 3: Parent true, branch true - SHOULD decrement
        runner.patch({"Mode": True})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == -1


class TestDynamicpresets:
    """Tests for dynamic presets (Tag references instead of literals)."""

    def test_ctu_with_dynamic_preset(self, runner_factory):
        """CTU supports Tag preset that can change at runtime."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        preset = Int("preset")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=preset).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False, "preset": 5})
        runner.step()

        # Count up to 4 - not done yet with preset=5
        _pulse(runner, "Trigger", 4)
        assert runner.current_state.tags["Counter_Acc"] == 4
        assert runner.current_state.tags["Counter_Done"] is False

        # Count one more - done at preset=5
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == 5
        assert runner.current_state.tags["Counter_Done"] is True

        # Change preset to 10 - done goes back to False with no count
        runner.patch({"preset": 10})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 5
        assert runner.current_state.tags["Counter_Done"] is False  # Not done anymore

        # Continue to new preset
        _pulse(runner, "Trigger", 5)
        assert runner.current_state.tags["Counter_Acc"] == 10
        assert runner.current_state.tags["Counter_Done"] is True  # Done again

    def test_ctd_with_dynamic_preset(self, runner_factory):
        """CTD supports Tag preset that can change at runtime."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        preset = Int("preset")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter[1], preset=preset).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False, "preset": -3})
        runner.step()

        # Reset to start at 0
        runner.patch({"ResetBtn": True})
        runner.step()
        runner.patch({"ResetBtn": False})
        runner.step()

        # Count down: 0 -> -1 -> -2 (not done yet with preset=-3)
        _pulse(runner, "Trigger", 2)
        assert runner.current_state.tags["Counter_Acc"] == -2
        assert runner.current_state.tags["Counter_Done"] is False

        # Count one more - done at -3 (acc <= preset)
        _pulse(runner, "Trigger")
        assert runner.current_state.tags["Counter_Acc"] == -3
        assert runner.current_state.tags["Counter_Done"] is True

        # Change preset to -5 - done goes back to False with no count
        runner.patch({"preset": -5})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == -3
        assert runner.current_state.tags["Counter_Done"] is False  # Not done anymore

        # Two more edges to -5 - done again
        _pulse(runner, "Trigger", 2)
        assert runner.current_state.tags["Counter_Acc"] == -5
        assert runner.current_state.tags["Counter_Done"] is True  # Done again

    def test_preset_decrease_affects_done_immediately(self, runner_factory):
        """When preset decreases below acc, done bit changes on the next scan it runs."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        preset = Int("preset")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter[1], preset=preset).reset(ResetBtn)

        runner = runner_factory(logic)
        runner.patch({"Trigger": False, "ResetBtn": False, "preset": 100})
        runner.step()

        # Count up to 10 - not done (preset=100)
        _pulse(runner, "Trigger", 10)
        assert runner.current_state.tags["Counter_Acc"] == 10
        assert runner.current_state.tags["Counter_Done"] is False

        # Decrease preset to 5 - done becomes True with no count
        runner.patch({"preset": 5})
        runner.step()
        assert runner.current_state.tags["Counter_Acc"] == 10
        assert runner.current_state.tags["Counter_Done"] is True  # Now done!


class TestCounterConditionTypeGuards:
    """Counter helper conditions remain BOOL-only for direct Tag inputs."""

    def test_count_up_reset_rejects_int_tag(self):
        Enable = Bool("Enable")
        ResetValue = Int("ResetValue")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    count_up(Counter[1], preset=5).reset(ResetValue)

    def test_count_up_down_rejects_int_tag(self):
        Enable = Bool("Enable")
        DownValue = Int("DownValue")
        Reset = Bool("Reset")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    count_up(Counter[1], preset=5).down(DownValue).reset(Reset)

    def test_count_down_reset_rejects_int_tag(self):
        Enable = Bool("Enable")
        ResetValue = Int("ResetValue")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    count_down(Counter[1], preset=5).reset(ResetValue)


class TestCounterStructuralContract:
    """Counter instructions accept any UDT with Done: Bool and Acc: Int|Dint."""

    def test_custom_udt_with_dint_acc_works(self, runner_factory):
        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        @udt()
        class MyCounter:
            Done: Bool  # noqa: F821
            Acc: Dint  # noqa: F821

        with Program() as logic:
            with Rung(Sensor):
                count_up(MyCounter[1], preset=5).reset(Reset)

        runner = runner_factory(logic)
        runner.patch({"Sensor": True, "Reset": False})
        runner.step()
        assert runner.current_state.tags["MyCounter_Acc"] == 1

    def test_custom_udt_with_int_acc_works(self, runner_factory):
        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        @udt()
        class SmallCounter:
            Done: Bool  # noqa: F821
            Acc: Int  # noqa: F821

        with Program() as logic:
            with Rung(Sensor):
                count_up(SmallCounter[1], preset=5).reset(Reset)

        runner = runner_factory(logic)
        runner.patch({"Sensor": True, "Reset": False})
        runner.step()
        assert runner.current_state.tags["SmallCounter_Acc"] == 1

    def test_rejects_non_bool_done(self):
        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        @udt()
        class BadDone:
            Done: Int  # noqa: F821
            Acc: Dint  # noqa: F821

        with Program():
            with Rung(Sensor):
                with pytest.raises(TypeError, match="'Done' to be a Bool"):
                    count_up(BadDone[1], preset=5).reset(Reset)

    def test_rejects_non_int_acc(self):
        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        @udt()
        class BadAcc:
            Done: Bool  # noqa: F821
            Acc: Real  # noqa: F821

        with Program():
            with Rung(Sensor):
                with pytest.raises(TypeError, match="'Acc' to be an Int or Dint"):
                    count_up(BadAcc[1], preset=5).reset(Reset)


class TestCounterStatusBits:
    """Test CU (Count Up) and CD (Count Down) status bits."""

    def test_ctu_status_bits(self, runner_factory):
        """CTU: CU=True when counting, CD=False always (no down condition)."""
        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        with Program() as logic:
            with Rung(Sensor):
                count_up(Counter[1], preset=3).reset(Reset)

        runner = runner_factory(logic)

        # Not counting: CU=False, CD=False
        runner.patch({"Sensor": False})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is False
        assert runner.current_state.tags["Counter_CD"] is False

        # Counting: CU=True, CD=False
        runner.patch({"Sensor": True})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is True
        assert runner.current_state.tags["Counter_CD"] is False
        assert runner.current_state.tags["Counter_Acc"] == 1

        # Reset: CU reflects rung state, CD=False
        runner.patch({"Sensor": False, "Reset": True})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is False
        assert runner.current_state.tags["Counter_CD"] is False
        assert runner.current_state.tags["Counter_Acc"] == 0

    def test_ctd_status_bits(self, runner_factory):
        """CTD: CD=True when counting down, CU=False always."""
        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        with Program() as logic:
            with Rung(Sensor):
                count_down(Counter[1], preset=3).reset(Reset)

        runner = runner_factory(logic)

        # Counting down: CU=False, CD=True
        runner.patch({"Sensor": True})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is False
        assert runner.current_state.tags["Counter_CD"] is True
        assert runner.current_state.tags["Counter_Acc"] == -1

        # Not counting: CU=False, CD=False
        runner.patch({"Sensor": False})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is False
        assert runner.current_state.tags["Counter_CD"] is False

    def test_bidirectional_status_bits(self, runner_factory):
        """Bidirectional CTU: CU and CD reflect their respective conditions."""
        Up = Bool("Up")
        Down = Bool("Down")
        Reset = Bool("Reset")

        with Program() as logic:
            with Rung(Up):
                count_up(Counter[1], preset=10).down(Down).reset(Reset)

        runner = runner_factory(logic)

        # Both active: the up and down edges cancel
        runner.patch({"Up": True, "Down": True})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is True
        assert runner.current_state.tags["Counter_CD"] is True
        assert runner.current_state.tags["Counter_Acc"] == 0  # +1 -1 = 0

        # Only up
        runner.patch({"Up": True, "Down": False})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is True
        assert runner.current_state.tags["Counter_CD"] is False

        # Only down
        runner.patch({"Up": False, "Down": True})
        runner.step()
        assert runner.current_state.tags["Counter_CU"] is False
        assert runner.current_state.tags["Counter_CD"] is True

    def test_custom_udt_without_status_bits(self, runner_factory):
        """Custom UDT without CU/CD works without errors."""

        @udt()
        class MyCounter:
            Done: bool
            Acc: int

        Sensor = Bool("Sensor")
        Reset = Bool("Reset")

        with Program() as logic:
            with Rung(Sensor):
                count_up(MyCounter, preset=5).reset(Reset)

        runner = runner_factory(logic)
        runner.patch({"Sensor": True})
        runner.step()
        assert runner.current_state.tags["MyCounter_Acc"] == 1
        assert "MyCounter_CU" not in runner.current_state.tags
        assert "MyCounter_CD" not in runner.current_state.tags
