"""Tests for Timer instructions (TON, TOF, RTON).

Timers are terminal instructions that accumulate elapsed time.
They manipulate both a done bit and an accumulator.

Timer Types:
- TON (on_delay without reset): Counts while enabled, resets when disabled
- RTON (on_delay with reset): Counts while enabled, holds when disabled, manual reset
- TOF (off_delay): Done=True while enabled, counts after disable, auto-resets

Hardware-verified behaviors (Click PLC):
- Accumulator updates IMMEDIATELY when instruction executes (mid-scan visible)
- Linear accumulation: acc += dt each scan while enabled
- First scan includes current scan's dt (not 0 on first enable)
"""

from pyrung.core import Bool, Int, PLCRunner, Program, Rung, TimeMode


class TestOnDelayTON:
    """Test On-Delay Timer (TON) - on_delay without .reset()."""

    def test_ton_accumulates_time_while_enabled(self):
        """TON accumulates elapsed time each scan while rung is true."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10ms per scan
        runner.patch({"Enable": False})
        runner.step()

        # Enable timer and run 5 scans (50ms)
        runner.patch({"Enable": True})
        for i in range(5):
            runner.step()
            # Each scan adds 10ms = 10 units (assuming Tms)
            expected = (i + 1) * 10
            assert runner.current_state.tags["td.Timer_acc"] == expected, (
                f"After {i + 1} scans, acc should be {expected}"
            )

    def test_ton_done_bit_when_setpoint_reached(self):
        """TON done bit turns ON when accumulator >= setpoint."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=50)  # 50ms setpoint

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10ms per scan
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})

        # Run 4 scans (40ms) - not done yet
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 40
        assert runner.current_state.tags["t.Timer"] is False

        # Run 1 more scan (50ms) - should be done
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 50
        assert runner.current_state.tags["t.Timer"] is True

        # Continue - done stays true, acc keeps counting
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 60
        assert runner.current_state.tags["t.Timer"] is True

    def test_ton_resets_immediately_when_disabled(self):
        """TON resets acc and done to 0/False immediately when rung goes false."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10ms per scan
        runner.patch({"Enable": False})
        runner.step()

        # Enable and accumulate some time
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30

        # Disable - should reset immediately
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 0
        assert runner.current_state.tags["t.Timer"] is False

    def test_ton_restarts_fresh_when_re_enabled(self):
        """TON starts from 0 when re-enabled after being disabled."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable, accumulate, disable
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 0

        # Re-enable - should start fresh
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 10  # First scan's dt


class TestOnDelayRTON:
    """Test Retentive On-Delay Timer (RTON) - on_delay with .reset()."""

    def test_rton_accumulates_time_while_enabled(self):
        """RTON accumulates elapsed time each scan while rung is true."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        runner.patch({"Enable": True})
        for i in range(5):
            runner.step()
            expected = (i + 1) * 10
            assert runner.current_state.tags["td.Timer_acc"] == expected

    def test_rton_holds_value_when_disabled(self):
        """RTON retains accumulated time when rung goes false."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Enable and accumulate
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30

        # Disable - acc should HOLD (not reset)
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30  # Still 30!

        # Multiple scans while disabled - still holds
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30

    def test_rton_continues_from_held_value_when_re_enabled(self):
        """RTON continues accumulating from held value when re-enabled."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Enable, accumulate 30ms, disable
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30

        # Re-enable - should continue from 30
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 40  # 30 + 10

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 50  # 40 + 10

    def test_rton_only_resets_via_reset_condition(self):
        """RTON only resets when reset condition is true."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=50).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Accumulate past setpoint (done = True)
        runner.patch({"Enable": True})
        for _ in range(6):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 60
        assert runner.current_state.tags["t.Timer"] is True

        # Disable - should hold
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 60
        assert runner.current_state.tags["t.Timer"] is True  # Done also holds

        # Activate reset - should clear
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 0
        assert runner.current_state.tags["t.Timer"] is False


class TestOffDelayTOF:
    """Test Off-Delay Timer (TOF) - off_delay without .reset()."""

    def test_tof_done_true_while_enabled(self):
        """TOF done bit is True while rung is true, acc stays at 0."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=50)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable timer
        runner.patch({"Enable": True})
        runner.step()

        # Done should be True, acc should be 0
        assert runner.current_state.tags["t.Timer"] is True
        assert runner.current_state.tags["td.Timer_acc"] == 0

        # Multiple scans while enabled - done stays True, acc stays 0
        for _ in range(5):
            runner.step()
        assert runner.current_state.tags["t.Timer"] is True
        assert runner.current_state.tags["td.Timer_acc"] == 0

    def test_tof_counts_after_disable_done_stays_true(self):
        """TOF counts up after disable, done stays True until setpoint."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=50)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Count up while disabled - done stays True
        for i in range(4):
            runner.step()
            expected = (i + 1) * 10
            assert runner.current_state.tags["td.Timer_acc"] == expected
            assert runner.current_state.tags["t.Timer"] is True  # Still True

    def test_tof_done_false_when_setpoint_reached(self):
        """TOF done goes False when acc >= setpoint after disable."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=50)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Count to just before setpoint (40ms)
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 40
        assert runner.current_state.tags["t.Timer"] is True

        # One more scan to reach setpoint (50ms)
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 50
        assert runner.current_state.tags["t.Timer"] is False  # Done goes False

    def test_tof_auto_resets_when_re_enabled(self):
        """TOF auto-resets (done=True, acc=0) when re-enabled."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=50)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable, disable, count partway
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30
        assert runner.current_state.tags["t.Timer"] is True

        # Re-enable - should reset
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 0
        assert runner.current_state.tags["t.Timer"] is True


class TestTimerIntegration:
    """Integration tests for timer instructions."""

    def test_timer_mid_scan_visible_to_later_rungs(self):
        """Timer accumulator updates mid-scan and is visible to later rungs.

        Hardware-verified: With 2ms fixed scan, CapturedAcc values were 2,4,6,8,10
        showing the timer updated BEFORE the capture rung executed.
        """
        from pyrung.core import copy

        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")
        CapturedAcc = Int("CapturedAcc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=1000)

            with Rung(Enable):
                copy(Timer_acc, CapturedAcc)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.002)  # 2ms per scan
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})
        runner.step()

        # CapturedAcc should have the UPDATED value (2ms), not the old value (0)
        assert runner.current_state.tags["CapturedAcc"] == 2, (
            "Timer should update mid-scan, visible to later rungs"
        )

        runner.step()
        assert runner.current_state.tags["CapturedAcc"] == 4

        runner.step()
        assert runner.current_state.tags["CapturedAcc"] == 6

    def test_timer_with_multiple_time_units(self):
        """Timer works with different time unit scaling.

        Note: This test assumes Tms (milliseconds) as default.
        Other units (Ts, Tm, Th, Td) scale accordingly.
        """
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                # Using default Tms - accumulator in milliseconds
                on_delay(Timer_done, Timer_acc, setpoint=100)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.025)  # 25ms per scan
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 25

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 50

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 75

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 100
        assert runner.current_state.tags["t.Timer"] is True  # Done at setpoint

    def test_ton_and_tof_in_same_program(self):
        """TON and TOF can coexist in the same program."""
        from pyrung.core import out

        Motor = Bool("Motor")
        TON_done = Bool("t.StartDelay")
        TON_acc = Int("td.StartDelay_acc")
        TOF_done = Bool("t.StopDelay")
        TOF_acc = Int("td.StopDelay_acc")
        MotorOutput = Bool("MotorOutput")

        with Program() as logic:
            # Start delay: Motor must be on for 50ms before output
            with Rung(Motor):
                from pyrung.core import on_delay

                on_delay(TON_done, TON_acc, setpoint=50)

            # Stop delay: Output stays on 50ms after motor stops
            with Rung(Motor):
                from pyrung.core import off_delay

                off_delay(TOF_done, TOF_acc, setpoint=50)

            # Output logic: TON done AND TOF done
            with Rung(TON_done, TOF_done):
                out(MotorOutput)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"StartBtn": False, "Motor": False})
        runner.step()

        # Start motor
        runner.patch({"Motor": True})

        # Wait for start delay (5 scans = 50ms)
        for _ in range(5):
            runner.step()

        assert runner.current_state.tags["t.StartDelay"] is True  # TON done
        assert runner.current_state.tags["t.StopDelay"] is True  # TOF done (enabled)
        assert runner.current_state.tags["MotorOutput"] is True

        # Stop motor - TOF starts counting, output should stay on
        runner.patch({"Motor": False})
        runner.step()

        # TON resets immediately
        assert runner.current_state.tags["t.StartDelay"] is False
        # TOF still true (off-delay)
        assert runner.current_state.tags["t.StopDelay"] is True
        # Output goes off because TON is false
        assert runner.current_state.tags["MotorOutput"] is False

    def test_pump_delay_scenario(self):
        """Real-world scenario: Pump runs 5 minutes after motor starts."""
        MotorRunning = Bool("MotorRunning")
        PumpReady = Bool("t.PumpTmr")
        PumpTmr_acc = Int("td.PumpTmr_acc")
        PumpOutput = Bool("PumpOutput")

        with Program() as logic:
            with Rung(MotorRunning):
                from pyrung.core import on_delay

                # 5000ms = 5 seconds (scaled down from 5 minutes for test)
                on_delay(PumpReady, PumpTmr_acc, setpoint=5000)

            with Rung(PumpReady):
                from pyrung.core import out

                out(PumpOutput)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=1.0)  # 1 second per scan
        runner.patch({"MotorRunning": False})
        runner.step()

        runner.patch({"MotorRunning": True})

        # After 4 seconds - not ready yet
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.PumpTmr_acc"] == 4000
        assert runner.current_state.tags["PumpOutput"] is False

        # After 5 seconds - ready
        runner.step()
        assert runner.current_state.tags["td.PumpTmr_acc"] == 5000
        assert runner.current_state.tags["t.PumpTmr"] is True
        assert runner.current_state.tags["PumpOutput"] is True

        # Motor stops - timer and output reset
        runner.patch({"MotorRunning": False})
        runner.step()
        assert runner.current_state.tags["td.PumpTmr_acc"] == 0
        assert runner.current_state.tags["PumpOutput"] is False


class TestDynamicSetpoints:
    """Tests for dynamic setpoints (Tag references instead of literals)."""

    def test_ton_with_dynamic_setpoint(self):
        """TON supports Tag setpoint that can change at runtime."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")
        Setpoint = Int("Setpoint")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=Setpoint)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10ms per scan
        runner.patch({"Enable": False, "Setpoint": 50})
        runner.step()

        # Enable timer
        runner.patch({"Enable": True})

        # Run 4 scans (40ms) - not done yet with setpoint=50
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 40
        assert runner.current_state.tags["t.Timer"] is False

        # Run 1 more scan (50ms) - done
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 50
        assert runner.current_state.tags["t.Timer"] is True

        # Change setpoint to 100 - done should go back to False
        runner.patch({"Setpoint": 100})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 60
        assert runner.current_state.tags["t.Timer"] is False  # Now not done

        # Continue until new setpoint
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 100
        assert runner.current_state.tags["t.Timer"] is True  # Done again

    def test_tof_setpoint_increase_after_timeout_re_enables_done(self):
        """TOF: If setpoint increases past acc after timeout, done goes True.

        This matches CLICK behavior per the manual warning:
        "After the Off-Delay Counter has been finished, if the Setpoint value
        is then changed to a value which is GREATER than the Current time value,
        then the output of the timer will come on again until the new, higher
        Setpoint value is reached."
        """
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")
        Setpoint = Int("Setpoint")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=Setpoint)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10ms per scan
        runner.patch({"Enable": False, "Setpoint": 50})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["t.Timer"] is True
        runner.patch({"Enable": False})

        # Count to setpoint (50ms = 5 scans)
        for _ in range(5):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 50
        assert runner.current_state.tags["t.Timer"] is False  # Timed out

        # Increase setpoint to 100 - done should go back to True
        runner.patch({"Setpoint": 100})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 60  # Resumed counting
        assert runner.current_state.tags["t.Timer"] is True  # Re-enabled!

        # Continue counting to new setpoint
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 100
        assert runner.current_state.tags["t.Timer"] is False  # Timed out again

    def test_rton_with_dynamic_setpoint(self):
        """RTON supports Tag setpoint."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")
        Setpoint = Int("Setpoint")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=Setpoint).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False, "Setpoint": 50})
        runner.step()

        # Accumulate, disable, hold
        runner.patch({"Enable": True})
        for _ in range(5):
            runner.step()
        assert runner.current_state.tags["t.Timer"] is True  # Done at 50

        # Change setpoint to 100 while still enabled
        runner.patch({"Setpoint": 100})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 60
        assert runner.current_state.tags["t.Timer"] is False  # Not done anymore

        # Continue to new setpoint
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 100
        assert runner.current_state.tags["t.Timer"] is True  # Done again


class TestTimerAccumulatorOverflow:
    """Tests for timer accumulator behavior approaching max int value.

    Hardware-verified: Timer accumulators continue counting past setpoint
    until they hit the maximum value for a 16-bit signed integer (32767),
    then clamp at that value.

    Click PLC TD registers are Single Word Integer: -32,768 to 32,767
    """

    INT16_MAX = 32767

    def test_ton_accumulates_past_setpoint(self):
        """TON accumulator continues past setpoint towards max int."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)  # 10ms per scan
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})

        # Run past setpoint
        for _ in range(15):
            runner.step()

        assert runner.current_state.tags["td.Timer_acc"] == 150
        assert runner.current_state.tags["t.Timer"] is True

        # Continue - accumulator keeps going past setpoint
        for _ in range(10):
            runner.step()

        assert runner.current_state.tags["td.Timer_acc"] == 250
        assert runner.current_state.tags["t.Timer"] is True

    def test_ton_accumulator_clamps_at_max_int(self):
        """TON accumulator clamps at max int value (32767).

        Uses large dt to reach max int quickly.
        """
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100)

        runner = PLCRunner(logic)
        # Use large dt (10 seconds per scan = 10000ms) to reach max int quickly
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=10.0)  # 10000ms per scan
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})

        # First scan adds 10000
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 10000

        # Second scan adds another 10000
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 20000

        # Third scan adds 10000 more
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30000

        # Fourth scan would go past 32767 - clamps at max
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 32767

        # Further scans stay clamped at max
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 32767

    def test_rton_accumulator_continues_past_setpoint(self):
        """RTON accumulator continues past setpoint when enabled."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=50).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        runner.patch({"Enable": True})

        # Run well past setpoint
        for _ in range(20):
            runner.step()

        assert runner.current_state.tags["td.Timer_acc"] == 200
        assert runner.current_state.tags["t.Timer"] is True

    def test_rton_accumulator_clamps_at_max_int(self):
        """RTON clamps at max int when re-enabled and continuing."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import on_delay

                on_delay(Timer_done, Timer_acc, setpoint=100).reset(ResetBtn)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=10.0)  # 10000ms per scan
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Enable, accumulate, disable (holds)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 10000

        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 10000  # Held

        # Re-enable - continues from held value
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 20000

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30000

        # Next scan would exceed 32767 - clamps at max
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 32767

    def test_tof_accumulator_continues_past_setpoint(self):
        """TOF accumulator continues counting past setpoint while disabled."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=50)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Run past setpoint
        for _ in range(10):
            runner.step()

        assert runner.current_state.tags["td.Timer_acc"] == 100
        assert runner.current_state.tags["t.Timer"] is False

    def test_tof_accumulator_clamps_at_max_int(self):
        """TOF accumulator clamps at max int value (32767) while disabled."""
        Enable = Bool("Enable")
        Timer_done = Bool("t.Timer")
        Timer_acc = Int("td.Timer_acc")

        with Program() as logic:
            with Rung(Enable):
                from pyrung.core import off_delay

                off_delay(Timer_done, Timer_acc, setpoint=50)

        runner = PLCRunner(logic)
        runner.set_time_mode(TimeMode.FIXED_STEP, dt=10.0)  # 10000ms per scan
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Accumulate large values
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 10000
        assert runner.current_state.tags["t.Timer"] is False  # Past setpoint

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 20000

        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 30000

        # Next scan would exceed 32767 - clamps at max
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 32767

        # Further scans stay clamped
        runner.step()
        assert runner.current_state.tags["td.Timer_acc"] == 32767
