"""Tests for Counter instructions (CTU, CTD).

Counters are edge-triggered instructions that must be the last in a rung (terminal).
They manipulate both a done bit and an accumulator.
"""

import pytest

from pyrung.core import Bool, Dint, Int, Program, Rung, count_down, count_up, latch, out, rise


class TestCountUpInstruction:
    """Test Count Up (CTU) instruction."""

    def test_count_up_increments_every_scan(self):
        """CTU increments accumulator EVERY SCAN when rung is true.

        Click behavior: NOT edge-triggered. Increments continuously while enabled.
        """
        PartSensor = Bool("PartSensor")
        ResetBtn = Bool("ResetBtn")
        PartCount_done = Bool("ct.PartCount")
        PartCount_acc = Dint("ctd.PartCount_acc")

        with Program() as logic:
            with Rung(PartSensor):
                count_up(PartCount_done, PartCount_acc, setpoint=5).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"PartSensor": False, "ResetBtn": False})
        runner.step()

        # Enable sensor and run 3 scans - should increment each scan
        runner.patch({"PartSensor": True})
        runner.step()
        assert runner.current_state.tags["ctd.PartCount_acc"] == 1

        runner.step()  # Still true - should increment again
        assert runner.current_state.tags["ctd.PartCount_acc"] == 2

        runner.step()  # Still true - should increment again
        assert runner.current_state.tags["ctd.PartCount_acc"] == 3

        # Disable sensor - should stop incrementing
        runner.patch({"PartSensor": False})
        runner.step()
        assert runner.current_state.tags["ctd.PartCount_acc"] == 3

    def test_count_up_sets_done_bit_at_setpoint(self):
        """CTU done bit turns ON when accumulator >= setpoint."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter_done, Counter_acc, setpoint=3).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Count up to setpoint - 3 scans with trigger enabled
        runner.patch({"Trigger": True})
        for _ in range(3):
            runner.step()

        # Should be at setpoint now
        assert runner.current_state.tags["ctd.Counter_acc"] == 3
        assert runner.current_state.tags["ct.Counter"] is True  # Done bit ON

        # Continue counting - done bit stays ON
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 4
        assert runner.current_state.tags["ct.Counter"] is True

    def test_count_up_reset(self):
        """CTU reset clears both done bit and accumulator."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter_done, Counter_acc, setpoint=5).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Count up some - 2 scans with trigger enabled
        runner.patch({"Trigger": True})
        runner.step()
        runner.step()

        assert runner.current_state.tags["ctd.Counter_acc"] == 2

        # Activate reset
        runner.patch({"ResetBtn": True})
        runner.step()

        assert runner.current_state.tags["ctd.Counter_acc"] == 0
        assert runner.current_state.tags["ct.Counter"] is False

    def test_count_up_with_down_bidirectional(self):
        """CTU with .down() creates bidirectional counter."""
        Enter = Bool("Enter")
        Exit = Bool("Exit")
        ResetBtn = Bool("ResetBtn")
        Zone_done = Bool("ct.Zone")
        Zone_acc = Dint("ctd.Zone_acc")

        with Program() as logic:
            with Rung(rise(Enter)):
                count_up(Zone_done, Zone_acc, setpoint=10).down(rise(Exit)).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Enter": False, "Exit": False, "ResetBtn": False})
        runner.step()

        # Increment on Enter rising edge
        runner.patch({"Enter": True})
        runner.step()
        assert runner.current_state.tags["ctd.Zone_acc"] == 1

        runner.patch({"Enter": False})
        runner.step()

        runner.patch({"Enter": True})
        runner.step()
        assert runner.current_state.tags["ctd.Zone_acc"] == 2

        # Decrement on Exit rising edge
        runner.patch({"Enter": False, "Exit": True})
        runner.step()
        assert runner.current_state.tags["ctd.Zone_acc"] == 1

        runner.patch({"Exit": False})
        runner.step()

        runner.patch({"Exit": True})
        runner.step()
        assert runner.current_state.tags["ctd.Zone_acc"] == 0


class TestCountDownInstruction:
    """Test Count Down (CTD) instruction."""

    def test_count_down_decrements_every_scan(self):
        """CTD decrements accumulator EVERY SCAN when rung is true.

        Click behavior: NOT edge-triggered. Decrements continuously while enabled.
        Starts at 0, counts down to negative values.
        """
        Dispense = Bool("Dispense")
        Reload = Bool("Reload")
        Remaining_done = Bool("ct.Remaining")
        Remaining_acc = Dint("ctd.Remaining_acc")

        with Program() as logic:
            with Rung(Dispense):
                count_down(Remaining_done, Remaining_acc, setpoint=5).reset(Reload)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Dispense": False, "Reload": False})
        runner.step()

        # Initialize with reset - should clear to 0
        runner.patch({"Reload": True})
        runner.step()
        assert runner.current_state.tags["ctd.Remaining_acc"] == 0

        runner.patch({"Reload": False})
        runner.step()

        # Enable dispense and run 3 scans - should decrement each scan
        runner.patch({"Dispense": True})
        runner.step()
        assert runner.current_state.tags["ctd.Remaining_acc"] == -1

        runner.step()  # Still true - should decrement again
        assert runner.current_state.tags["ctd.Remaining_acc"] == -2

        runner.step()  # Still true - should decrement again
        assert runner.current_state.tags["ctd.Remaining_acc"] == -3

        # Disable dispense - should stop decrementing
        runner.patch({"Dispense": False})
        runner.step()
        assert runner.current_state.tags["ctd.Remaining_acc"] == -3

    def test_count_down_sets_done_bit_at_negative_setpoint(self):
        """CTD done bit turns ON when accumulator <= -setpoint.

        Click behavior: Done bit activates when reaching negative setpoint value.
        """
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter_done, Counter_acc, setpoint=3).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Initialize - should clear to 0
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 0
        assert runner.current_state.tags["ct.Counter"] is False

        runner.patch({"ResetBtn": False})
        runner.step()

        # Count down: 0 → -1 → -2 (not done yet)
        runner.patch({"Trigger": True})
        for _ in range(2):
            runner.step()

        assert runner.current_state.tags["ctd.Counter_acc"] == -2
        assert runner.current_state.tags["ct.Counter"] is False  # Not done yet

        # Count one more time to reach -3 (done!)
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -3
        assert runner.current_state.tags["ct.Counter"] is True  # Done bit ON

        # Continue counting - done bit stays ON
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -4
        assert runner.current_state.tags["ct.Counter"] is True

    def test_count_down_reset_clears_to_zero(self):
        """CTD reset clears accumulator to 0.

        Click behavior: Reset sets acc to 0, not to setpoint.
        """
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter_done, Counter_acc, setpoint=10).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Count down some: 0 → -1 → -2
        runner.patch({"ResetBtn": True})
        runner.step()
        runner.patch({"ResetBtn": False, "Trigger": True})
        runner.step()
        runner.step()

        assert runner.current_state.tags["ctd.Counter_acc"] == -2

        # Activate reset - should clear to 0
        runner.patch({"ResetBtn": True})
        runner.step()

        assert runner.current_state.tags["ctd.Counter_acc"] == 0
        assert runner.current_state.tags["ct.Counter"] is False


class TestCounterAccumulatorClamp:
    """Tests for DINT clamp behavior in counter accumulators."""

    def test_ctu_accumulator_clamps_at_dint_max(self):
        """CTU accumulator saturates at DINT max (2147483647)."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter_done, Counter_acc, setpoint=2147483647).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Prime to one below max, then increment into max
        runner.patch({"Trigger": True, "ctd.Counter_acc": 2147483646})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 2147483647
        assert runner.current_state.tags["ct.Counter"] is True

        # Further increments stay clamped
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 2147483647
        assert runner.current_state.tags["ct.Counter"] is True

    def test_ctd_accumulator_clamps_at_dint_min(self):
        """CTD accumulator saturates at DINT min (-2147483648)."""
        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter_done, Counter_acc, setpoint=1).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Prime to one above min, then decrement into min
        runner.patch({"Trigger": True, "ctd.Counter_acc": -2147483647})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -2147483648
        assert runner.current_state.tags["ct.Counter"] is True

        # Further decrements stay clamped
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -2147483648
        assert runner.current_state.tags["ct.Counter"] is True

    def test_ctu_bidirectional_clamp_applies_after_net_delta(self):
        """Bidirectional CTU clamps after applying net (+1/-1) scan delta."""
        Enable = Bool("Enable")
        Down = Bool("Down")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Enable):
                count_up(Counter_done, Counter_acc, setpoint=2147483647).down(Down).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Enable": False, "Down": False, "ResetBtn": False})
        runner.step()

        # With both UP and DOWN true at max, net delta is zero (stays at max)
        runner.patch({"Enable": True, "Down": True, "ctd.Counter_acc": 2147483647})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 2147483647

        # DOWN-only scan still decrements from the clamped value
        runner.patch({"Enable": False, "Down": True})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 2147483646


class TestCounterIntegration:
    """Integration tests for counter instructions."""

    def test_counter_accumulates_mid_scan_visible_to_later_rungs(self):
        """Counter should accumulate during the scan and be visible to later rungs.

        This test verifies that when a counter increments in one rung, subsequent rungs
        in the SAME scan can see the updated accumulator value.

        Based on physical CLICK PLC testing (TEST_SCRATCHPAD.md):
        - Counter increments immediately when its rung executes
        - Later rungs can check the accumulator value and execute based on it
        - This all happens within a single scan cycle
        """
        from pyrung.core import copy

        # Tags
        DataTest = Int("DataTest")
        TestCounter_done = Bool("ct.TestCounter")
        TestCounter_acc = Dint("ctd.TestCounter_acc")
        CopiedCounterBeforeEnd = Int("CopiedCounterBeforeEnd")
        Val2MultiplyInPlace = Int("Val2MultiplyInPlace")

        with Program() as logic:
            # Rung 1: Count up when DataTest == 1, reset when DataTest == 2
            with Rung(DataTest == 1):
                count_up(TestCounter_done, TestCounter_acc, setpoint=10).reset(DataTest == 2)

            # Rung 2: When counter acc reaches 1, do operations (mid-scan!)
            # This rung should execute IN THE SAME SCAN as the counter increment
            with Rung(DataTest == 1, TestCounter_acc == 1):
                copy(TestCounter_acc, CopiedCounterBeforeEnd)
                copy(10, Val2MultiplyInPlace)
                # User note: In physical test, this was math operations (* 10 twice)
                # We'll just copy values to verify execution

            # Rung 3: Change DataTest to 2 (will reset counter next scan)
            with Rung(DataTest == 1):
                copy(2, DataTest)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"DataTest": 1, "ResetBtn": False})

        # Execute 1 scan
        runner.step()

        # Expected behavior after 1 scan (based on physical CLICK test):
        # - Counter incremented to 1 during the scan
        # - Rung 2 saw TestCounter_acc == 1 and executed
        # - CopiedCounterBeforeEnd captured the counter value (1)
        # - Val2MultiplyInPlace was set to 10
        # - DataTest changed to 2 at end of scan
        assert runner.current_state.tags["ctd.TestCounter_acc"] == 1, (
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
        assert runner.current_state.tags["ctd.TestCounter_acc"] == 0, (
            "Counter should reset to 0 on second scan (DataTest == 2)"
        )
        assert runner.current_state.tags["ct.TestCounter"] is False, (
            "Counter done bit should be false after reset"
        )

    def test_counter_fires_mid_scan_current_implementation(self):
        """Verify our current implementation: does counter 'fire' mid-scan?

        This test explicitly checks if our implementation allows later rungs
        to see counter updates within the same scan. This is the expected behavior
        based on physical CLICK PLC testing.

        After running this test, user will verify against physical CLICK PLC.
        """
        Trigger = Bool("Trigger")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")
        ResetBtn = Bool("ResetBtn")
        SawCounterAt1 = Bool("SawCounterAt1")
        SawCounterAt2 = Bool("SawCounterAt2")

        with Program() as logic:
            # Rung 1: Count up every scan when Trigger is true
            with Rung(Trigger):
                count_up(Counter_done, Counter_acc, setpoint=5).reset(ResetBtn)

            # Rung 2: Set flag if we see counter == 1
            with Rung(Counter_acc == 1):
                latch(SawCounterAt1)

            # Rung 3: Set flag if we see counter == 2
            with Rung(Counter_acc == 2):
                latch(SawCounterAt2)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False})
        runner.step()

        # Scan 1: Enable trigger - counter should increment to 1
        runner.patch({"Trigger": True})
        runner.step()

        # Check: Did we see the counter at 1 in the SAME scan?
        assert runner.current_state.tags["ctd.Counter_acc"] == 1, (
            "Counter should be at 1 after first scan"
        )
        assert runner.current_state.tags["SawCounterAt1"] is True, (
            "Should have seen counter == 1 mid-scan (Rung 2 executed same scan as increment)"
        )

        # Scan 2: Trigger still true - counter should increment to 2
        runner.step()

        # Check: Did we see the counter at 2 in the SAME scan?
        assert runner.current_state.tags["ctd.Counter_acc"] == 2, (
            "Counter should be at 2 after second scan"
        )
        assert runner.current_state.tags["SawCounterAt2"] is True, (
            "Should have seen counter == 2 mid-scan (Rung 3 executed same scan as increment)"
        )

    def test_counter_in_production_line(self):
        """Test counter used in a production line scenario."""
        PartSensor = Bool("PartSensor")
        BatchComplete = Bool("BatchComplete")
        ResetButton = Bool("ResetButton")
        HalfwayLight = Bool("HalfwayLight")
        PartCount_done = Bool("ct.PartCount")
        PartCount_acc = Dint("ctd.PartCount_acc")

        with Program() as logic:
            # Count parts
            with Rung(rise(PartSensor)):
                count_up(PartCount_done, PartCount_acc, setpoint=100).reset(ResetButton)

            # Batch complete output
            with Rung(PartCount_done):
                out(BatchComplete)

            # Halfway indicator
            with Rung(PartCount_acc >= 50):
                out(HalfwayLight)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
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

        assert runner.current_state.tags["ctd.PartCount_acc"] == 50
        assert runner.current_state.tags["HalfwayLight"] is True
        assert runner.current_state.tags["BatchComplete"] is False

        # Count up to 100
        for _ in range(50):
            runner.patch({"PartSensor": True})
            runner.step()
            runner.patch({"PartSensor": False})
            runner.step()

        assert runner.current_state.tags["ctd.PartCount_acc"] == 100
        assert runner.current_state.tags["ct.PartCount"] is True
        assert runner.current_state.tags["BatchComplete"] is True

    def test_counter_in_branch_requires_parent_and_branch_conditions(self):
        """Counter in branch should require BOTH parent rung AND branch conditions."""
        from pyrung.core.program import branch

        Step = Int("Step")
        AutoMode = Bool("AutoMode")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        with Program() as logic:
            with Rung(Step == 0):
                with branch(AutoMode):
                    count_up(Counter_done, Counter_acc, setpoint=5).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Step": 0, "AutoMode": False, "ResetBtn": False})
        runner.step()

        # Case 1: Parent true, branch false - should NOT increment
        runner.patch({"AutoMode": True})  # Rising edge on AutoMode
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 1  # Increments

        runner.patch({"AutoMode": False})
        runner.step()

        # Case 2: Parent false, branch true - should NOT increment
        runner.patch({"Step": 1, "AutoMode": True})  # Parent false, branch has rising edge
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 1  # Should stay at 1

        # Case 3: Parent true, branch true - SHOULD increment
        # Need to clear AutoMode first, then make both conditions true together
        runner.patch({"AutoMode": False})  # Clear branch condition first
        runner.step()
        runner.patch({"Step": 0, "AutoMode": True})  # Now both true (rising edge on combined)
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 2

    def test_count_down_in_branch_requires_parent_and_branch_conditions(self):
        """Count down in branch should require BOTH parent rung AND branch conditions."""
        Enable = Bool("Enable")
        Mode = Bool("Mode")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")

        from pyrung.core.program import branch

        with Program() as logic:
            with Rung(Enable):
                with branch(Mode):
                    count_down(Counter_done, Counter_acc, setpoint=10).reset(ResetBtn)

        from pyrung.core import PLCRunner

        runner = PLCRunner(logic)
        runner.patch({"Enable": False, "Mode": False, "ResetBtn": False})
        runner.step()

        # Initialize with reset - clears to 0
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 0

        runner.patch({"ResetBtn": False})
        runner.step()

        # Case 1: Parent false, branch true - should NOT decrement
        runner.patch({"Enable": False, "Mode": True})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 0  # Should stay at 0

        # Case 2: Parent true, branch false - should NOT decrement
        runner.patch({"Enable": True, "Mode": False})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 0  # Should stay at 0

        # Case 3: Parent true, branch true - SHOULD decrement
        runner.patch({"Mode": True})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -1


class TestDynamicSetpoints:
    """Tests for dynamic setpoints (Tag references instead of literals)."""

    def test_ctu_with_dynamic_setpoint(self):
        """CTU supports Tag setpoint that can change at runtime."""
        from pyrung.core import PLCRunner

        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")
        Setpoint = Int("Setpoint")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter_done, Counter_acc, setpoint=Setpoint).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False, "Setpoint": 5})
        runner.step()

        # Count up to 4 - not done yet with setpoint=5
        runner.patch({"Trigger": True})
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 4
        assert runner.current_state.tags["ct.Counter"] is False

        # Count one more - done at setpoint=5
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 5
        assert runner.current_state.tags["ct.Counter"] is True

        # Change setpoint to 10 - done should go back to False
        runner.patch({"Setpoint": 10})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 6
        assert runner.current_state.tags["ct.Counter"] is False  # Not done anymore

        # Continue to new setpoint
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 10
        assert runner.current_state.tags["ct.Counter"] is True  # Done again

    def test_ctd_with_dynamic_setpoint(self):
        """CTD supports Tag setpoint that can change at runtime."""
        from pyrung.core import PLCRunner

        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")
        Setpoint = Int("Setpoint")

        with Program() as logic:
            with Rung(Trigger):
                count_down(Counter_done, Counter_acc, setpoint=Setpoint).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False, "Setpoint": 3})
        runner.step()

        # Reset to start at 0
        runner.patch({"ResetBtn": True})
        runner.step()
        runner.patch({"ResetBtn": False})
        runner.step()

        # Count down: 0 → -1 → -2 (not done yet with setpoint=3)
        runner.patch({"Trigger": True})
        for _ in range(2):
            runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -2
        assert runner.current_state.tags["ct.Counter"] is False

        # Count one more - done at -3 (meets -setpoint)
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -3
        assert runner.current_state.tags["ct.Counter"] is True

        # Change setpoint to 5 - done should go back to False
        runner.patch({"Setpoint": 5})
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -4
        assert runner.current_state.tags["ct.Counter"] is False  # Not done anymore

        # Count one more to -5 - done again
        runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == -5
        assert runner.current_state.tags["ct.Counter"] is True  # Done again

    def test_setpoint_decrease_affects_done_immediately(self):
        """When setpoint decreases below acc, done bit changes immediately."""
        from pyrung.core import PLCRunner

        Trigger = Bool("Trigger")
        ResetBtn = Bool("ResetBtn")
        Counter_done = Bool("ct.Counter")
        Counter_acc = Dint("ctd.Counter_acc")
        Setpoint = Int("Setpoint")

        with Program() as logic:
            with Rung(Trigger):
                count_up(Counter_done, Counter_acc, setpoint=Setpoint).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.patch({"Trigger": False, "ResetBtn": False, "Setpoint": 100})
        runner.step()

        # Count up to 10 - not done (setpoint=100)
        runner.patch({"Trigger": True})
        for _ in range(10):
            runner.step()
        assert runner.current_state.tags["ctd.Counter_acc"] == 10
        assert runner.current_state.tags["ct.Counter"] is False

        # Decrease setpoint to 5 - done should become True immediately
        runner.patch({"Setpoint": 5})
        runner.step()  # Acc goes to 11, but setpoint is now 5
        assert runner.current_state.tags["ctd.Counter_acc"] == 11
        assert runner.current_state.tags["ct.Counter"] is True  # Now done!


class TestCounterConditionTypeGuards:
    """Counter helper conditions remain BOOL-only for direct Tag inputs."""

    def test_count_up_reset_rejects_int_tag(self):
        Enable = Bool("Enable")
        Done = Bool("ct.Done")
        Acc = Dint("ctd.Acc")
        ResetValue = Int("ResetValue")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    count_up(Done, Acc, setpoint=5).reset(ResetValue)

    def test_count_up_down_rejects_int_tag(self):
        Enable = Bool("Enable")
        Done = Bool("ct.Done")
        Acc = Dint("ctd.Acc")
        DownValue = Int("DownValue")
        Reset = Bool("Reset")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    count_up(Done, Acc, setpoint=5).down(DownValue).reset(Reset)

    def test_count_down_reset_rejects_int_tag(self):
        Enable = Bool("Enable")
        Done = Bool("ct.Done")
        Acc = Dint("ctd.Acc")
        ResetValue = Int("ResetValue")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    count_down(Done, Acc, setpoint=5).reset(ResetValue)
