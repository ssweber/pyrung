"""Tests for Timer instructions (TON, TOF, RTON).

Timers manipulate both a done bit and an accumulator.
Only RTON (`on_delay(...).reset(...)`) is terminal in-flow.

Timer Types:
- TON (on_delay without reset): Counts while enabled, resets when disabled
- RTON (on_delay with reset): Counts while enabled, holds when disabled, manual reset
- TOF (off_delay): Done=True while enabled, counts after disable, auto-resets

Hardware-verified behaviors (Click PLC):
- Accumulator updates IMMEDIATELY when instruction executes (mid-scan visible)
- Linear accumulation: acc += dt each scan while enabled
- First scan includes current scan's dt (not 0 on first enable)
"""

import pytest

from pyrung.core import (
    Bool,
    Dint,
    Field,
    Int,
    Program,
    Real,
    Rung,
    Timer,
    copy,
    off_delay,
    on_delay,
    out,
    udt,
)

Timer2 = Timer.clone("Timer2")


class TestOnDelayTON:
    """Test On-Delay Timer (TON) - on_delay without .reset()."""

    def test_ton_accumulates_time_while_enabled(self, runner_factory):
        """TON accumulates elapsed time each scan while rung is true."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable timer and run 5 scans (50ms)
        runner.patch({"Enable": True})
        for i in range(5):
            runner.step()
            # Each scan adds 10ms = 10 units (assuming Tms)
            expected = (i + 1) * 10
            assert runner.current_state.tags["Timer_Acc"] == expected, (
                f"After {i + 1} scans, acc should be {expected}"
            )

    def test_ton_done_bit_when_preset_reached(self, runner_factory):
        """TON done bit turns ON when accumulator >= preset."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=50)  # 50ms preset

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})

        # Run 4 scans (40ms) - not done yet
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 40
        assert runner.current_state.tags["Timer_Done"] is False

        # Run 1 more scan (50ms) - should be done
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 50
        assert runner.current_state.tags["Timer_Done"] is True

        # Continue - done stays true, acc keeps counting
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 60
        assert runner.current_state.tags["Timer_Done"] is True

    def test_ton_resets_immediately_when_disabled(self, runner_factory):
        """TON resets acc and done to 0/False immediately when rung goes false."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable and accumulate some time
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30

        # Disable - should reset immediately
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 0
        assert runner.current_state.tags["Timer_Done"] is False

    def test_ton_restarts_fresh_when_re_enabled(self, runner_factory):
        """TON starts from 0 when re-enabled after being disabled."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable, accumulate, disable
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 0

        # Re-enable - should start fresh
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10  # First scan's dt


class TestOnDelayRTON:
    """Test Retentive On-Delay Timer (RTON) - on_delay with .reset()."""

    def test_rton_accumulates_time_while_enabled(self, runner_factory):
        """RTON accumulates elapsed time each scan while rung is true."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        runner.patch({"Enable": True})
        for i in range(5):
            runner.step()
            expected = (i + 1) * 10
            assert runner.current_state.tags["Timer_Acc"] == expected

    def test_rton_holds_value_when_disabled(self, runner_factory):
        """RTON retains accumulated time when rung goes false."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Enable and accumulate
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30

        # Disable - acc should HOLD (not reset)
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30  # Still 30!

        # Multiple scans while disabled - still holds
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30

    def test_rton_continues_from_held_value_when_re_enabled(self, runner_factory):
        """RTON continues accumulating from held value when re-enabled."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Enable, accumulate 30ms, disable
        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30

        # Re-enable - should continue from 30
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 40  # 30 + 10

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 50  # 40 + 10

    def test_rton_only_resets_via_reset_condition(self, runner_factory):
        """RTON only resets when reset condition is true."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=50).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Accumulate past preset (done = True)
        runner.patch({"Enable": True})
        for _ in range(6):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 60
        assert runner.current_state.tags["Timer_Done"] is True

        # Disable - should hold
        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 60
        assert runner.current_state.tags["Timer_Done"] is True  # Done also holds

        # Activate reset - should clear
        runner.patch({"ResetBtn": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 0
        assert runner.current_state.tags["Timer_Done"] is False

    def test_rton_reset_condition_uses_rung_entry_snapshot(self, runner_factory):
        """Same-rung writes do not trip the helper reset until the next snapshot."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                copy(True, ResetBtn)
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True, "ResetBtn": False})

        runner.step()
        assert runner.current_state.tags["ResetBtn"] is True
        assert runner.current_state.tags["Timer_Acc"] == 10

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 0
        assert runner.current_state.tags["Timer_Done"] is False

    def test_rton_retentive_accumulator_survives_stop_to_run_transition(self, runner_factory):
        """RTON accumulator preserves value across STOP->RUN when retentive."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30

        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30

        runner.stop()
        runner.step()  # auto STOP->RUN transition + first scan

        assert runner.current_state.tags["Timer_Acc"] == 30

    def test_rton_non_retentive_accumulator_uses_default_after_batteryless_reboot(
        self, runner_factory
    ):
        """Batteryless reboot resets non-retentive RTON acc to tag default."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        @udt(count=1)
        class NRTimer:
            Done: Bool  # noqa: F821
            Acc: Int = Field(retentive=False, default=10)  # noqa: F821

        with Program() as logic:
            with Rung(Enable):
                on_delay(NRTimer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        runner.patch({"Enable": True})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["NRTimer_Acc"] == 40

        runner.battery_present = False
        runner.reboot()

        # After SRAM loss, tag value is rebuilt from its default.
        assert runner.current_state.tags["NRTimer_Acc"] == 10

        # First enabled scan continues counting from default seed.
        runner.patch({"Enable": True, "ResetBtn": False})
        runner.step()
        assert runner.current_state.tags["NRTimer_Acc"] == 20


class TestOffDelayTOF:
    """Test Off-Delay Timer (TOF) - off_delay without .reset()."""

    def test_tof_done_true_while_enabled(self, runner_factory):
        """TOF done bit is True while rung is true, acc stays at 0."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=50)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable timer
        runner.patch({"Enable": True})
        runner.step()

        # Done should be True, acc should be 0
        assert runner.current_state.tags["Timer_Done"] is True
        assert runner.current_state.tags["Timer_Acc"] == 0

        # Multiple scans while enabled - done stays True, acc stays 0
        for _ in range(5):
            runner.step()
        assert runner.current_state.tags["Timer_Done"] is True
        assert runner.current_state.tags["Timer_Acc"] == 0

    def test_tof_counts_after_disable_done_stays_true(self, runner_factory):
        """TOF counts up after disable, done stays True until preset."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=50)

        runner = runner_factory(logic, dt=0.010)
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
            assert runner.current_state.tags["Timer_Acc"] == expected
            assert runner.current_state.tags["Timer_Done"] is True  # Still True

    def test_tof_done_false_when_preset_reached(self, runner_factory):
        """TOF done goes False when acc >= preset after disable."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=50)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Count to just before preset (40ms)
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 40
        assert runner.current_state.tags["Timer_Done"] is True

        # One more scan to reach preset (50ms)
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 50
        assert runner.current_state.tags["Timer_Done"] is False  # Done goes False

    def test_tof_auto_resets_when_re_enabled(self, runner_factory):
        """TOF auto-resets (done=True, acc=0) when re-enabled."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=50)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable, disable, count partway
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})
        for _ in range(3):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30
        assert runner.current_state.tags["Timer_Done"] is True

        # Re-enable - should reset
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 0
        assert runner.current_state.tags["Timer_Done"] is True


class TestTimerIntegration:
    """Integration tests for timer instructions."""

    def test_timer_mid_scan_visible_to_later_rungs(self, runner_factory):
        """Timer accumulator updates mid-scan and is visible to later rungs.

        Hardware-verified: With 2ms fixed scan, CapturedAcc values were 2,4,6,8,10
        showing the timer updated BEFORE the capture rung executed.
        """

        Enable = Bool("Enable")
        CapturedAcc = Int("CapturedAcc")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=1000)

            with Rung(Enable):
                copy(Timer[1].Acc, CapturedAcc)

        runner = runner_factory(logic, dt=0.002)
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

    def test_timer_with_multiple_time_units(self, runner_factory):
        """Timer works with different time unit scaling.

        Note: This test assumes Tms (milliseconds) as default.
        Other units (Ts, Tm, Th, Td) scale accordingly.
        """
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                # Using default Tms - accumulator in milliseconds
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=0.025)
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 25

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 50

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 75

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 100
        assert runner.current_state.tags["Timer_Done"] is True  # Done at preset

    def test_ton_and_tof_in_same_program(self, runner_factory):
        """TON and TOF can coexist in the same program."""

        Motor = Bool("Motor")
        MotorOutput = Bool("MotorOutput")

        with Program() as logic:
            # Start delay: Motor must be on for 50ms before output
            with Rung(Motor):
                on_delay(Timer[1], preset=50)

            # Stop delay: Output stays on 50ms after motor stops
            with Rung(Motor):
                off_delay(Timer2, preset=50)

            # Output logic: TON done AND TOF done
            with Rung(Timer[1].Done, Timer2.Done):
                out(MotorOutput)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"StartBtn": False, "Motor": False})
        runner.step()

        # Start motor
        runner.patch({"Motor": True})

        # Wait for start delay (5 scans = 50ms)
        for _ in range(5):
            runner.step()

        assert runner.current_state.tags["Timer_Done"] is True  # TON done
        assert runner.current_state.tags["Timer2_Done"] is True  # TOF done (enabled)
        assert runner.current_state.tags["MotorOutput"] is True

        # Stop motor - TOF starts counting, output should stay on
        runner.patch({"Motor": False})
        runner.step()

        # TON resets immediately
        assert runner.current_state.tags["Timer_Done"] is False
        # TOF still true (off-delay)
        assert runner.current_state.tags["Timer2_Done"] is True
        # Output goes off because TON is false
        assert runner.current_state.tags["MotorOutput"] is False

    def test_pump_delay_scenario(self, runner_factory):
        """Real-world scenario: Pump runs 5 minutes after motor starts."""
        MotorRunning = Bool("MotorRunning")
        PumpOutput = Bool("PumpOutput")

        with Program() as logic:
            with Rung(MotorRunning):
                # 5000ms = 5 seconds (scaled down from 5 minutes for test)
                on_delay(Timer[1], preset=5000)

            with Rung(Timer[1].Done):
                out(PumpOutput)

        runner = runner_factory(logic, dt=1.0)
        runner.patch({"MotorRunning": False})
        runner.step()

        runner.patch({"MotorRunning": True})

        # After 4 seconds - not ready yet
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 4000
        assert runner.current_state.tags["PumpOutput"] is False

        # After 5 seconds - ready
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 5000
        assert runner.current_state.tags["Timer_Done"] is True
        assert runner.current_state.tags["PumpOutput"] is True

        # Motor stops - timer and output reset
        runner.patch({"MotorRunning": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 0
        assert runner.current_state.tags["PumpOutput"] is False


class TestDynamicpresets:
    """Tests for dynamic presets (Tag references instead of literals)."""

    def test_ton_with_dynamic_preset(self, runner_factory):
        """TON supports Tag preset that can change at runtime."""
        Enable = Bool("Enable")
        preset = Int("preset")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=preset)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "preset": 50})
        runner.step()

        # Enable timer
        runner.patch({"Enable": True})

        # Run 4 scans (40ms) - not done yet with preset=50
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 40
        assert runner.current_state.tags["Timer_Done"] is False

        # Run 1 more scan (50ms) - done
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 50
        assert runner.current_state.tags["Timer_Done"] is True

        # Change preset to 100 - done should go back to False
        runner.patch({"preset": 100})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 60
        assert runner.current_state.tags["Timer_Done"] is False  # Now not done

        # Continue until new preset
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 100
        assert runner.current_state.tags["Timer_Done"] is True  # Done again

    def test_tof_preset_increase_after_timeout_re_enables_done(self, runner_factory):
        """TOF: If preset increases past acc after timeout, done goes True.

        This matches CLICK behavior per the manual warning:
        "After the Off-Delay Counter has been finished, if the preset value
        is then changed to a value which is GREATER than the Current time value,
        then the output of the timer will come on again until the new, higher
        preset value is reached."
        """
        Enable = Bool("Enable")
        preset = Int("preset")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=preset)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "preset": 50})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Done"] is True
        runner.patch({"Enable": False})

        # Count to preset (50ms = 5 scans)
        for _ in range(5):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 50
        assert runner.current_state.tags["Timer_Done"] is False  # Timed out

        # Increase preset to 100 - done should go back to True
        runner.patch({"preset": 100})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 60  # Resumed counting
        assert runner.current_state.tags["Timer_Done"] is True  # Re-enabled!

        # Continue counting to new preset
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 100
        assert runner.current_state.tags["Timer_Done"] is False  # Timed out again

    def test_rton_with_dynamic_preset(self, runner_factory):
        """RTON supports Tag preset."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")
        preset = Int("preset")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=preset).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False, "preset": 50})
        runner.step()

        # Accumulate, disable, hold
        runner.patch({"Enable": True})
        for _ in range(5):
            runner.step()
        assert runner.current_state.tags["Timer_Done"] is True  # Done at 50

        # Change preset to 100 while still enabled
        runner.patch({"preset": 100})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 60
        assert runner.current_state.tags["Timer_Done"] is False  # Not done anymore

        # Continue to new preset
        for _ in range(4):
            runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 100
        assert runner.current_state.tags["Timer_Done"] is True  # Done again


class TestTimerAccumulatorOverflow:
    """Tests for timer accumulator behavior approaching max int value.

    Hardware-verified: Timer accumulators continue counting past preset
    until they hit the maximum value for a 16-bit signed integer (32767),
    then clamp at that value.

    Click PLC TD registers are Single Word Integer: -32,768 to 32,767
    """

    INT16_MAX = 32767

    def test_ton_accumulates_past_preset(self, runner_factory):
        """TON accumulator continues past preset towards max int."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})

        # Run past preset
        for _ in range(15):
            runner.step()

        assert runner.current_state.tags["Timer_Acc"] == 150
        assert runner.current_state.tags["Timer_Done"] is True

        # Continue - accumulator keeps going past preset
        for _ in range(10):
            runner.step()

        assert runner.current_state.tags["Timer_Acc"] == 250
        assert runner.current_state.tags["Timer_Done"] is True

    def test_ton_accumulator_clamps_at_max_int(self, runner_factory):
        """TON accumulator clamps at max int value (32767).

        Uses large dt to reach max int quickly.
        """
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=10.0)  # 10000ms per scan — reach max int quickly
        runner.patch({"Enable": False})
        runner.step()

        runner.patch({"Enable": True})

        # First scan adds 10000
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10000

        # Second scan adds another 10000
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 20000

        # Third scan adds 10000 more
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30000

        # Fourth scan would go past 32767 - clamps at max
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 32767

        # Further scans stay clamped at max
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 32767

    def test_rton_accumulator_continues_past_preset(self, runner_factory):
        """RTON accumulator continues past preset when enabled."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=50).reset(ResetBtn)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        runner.patch({"Enable": True})

        # Run well past preset
        for _ in range(20):
            runner.step()

        assert runner.current_state.tags["Timer_Acc"] == 200
        assert runner.current_state.tags["Timer_Done"] is True

    def test_rton_accumulator_clamps_at_max_int(self, runner_factory):
        """RTON clamps at max int when re-enabled and continuing."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100).reset(ResetBtn)

        runner = runner_factory(logic, dt=10.0)
        runner.patch({"Enable": False, "ResetBtn": False})
        runner.step()

        # Enable, accumulate, disable (holds)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10000

        runner.patch({"Enable": False})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10000  # Held

        # Re-enable - continues from held value
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 20000

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30000

        # Next scan would exceed 32767 - clamps at max
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 32767

    def test_tof_accumulator_continues_past_preset(self, runner_factory):
        """TOF accumulator continues counting past preset while disabled."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=50)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Run past preset
        for _ in range(10):
            runner.step()

        assert runner.current_state.tags["Timer_Acc"] == 100
        assert runner.current_state.tags["Timer_Done"] is False

    def test_tof_accumulator_clamps_at_max_int(self, runner_factory):
        """TOF accumulator clamps at max int value (32767) while disabled."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], preset=50)

        runner = runner_factory(logic, dt=10.0)
        runner.patch({"Enable": False})
        runner.step()

        # Enable then disable
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Enable": False})

        # Accumulate large values
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10000
        assert runner.current_state.tags["Timer_Done"] is False  # Past preset

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 20000

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 30000

        # Next scan would exceed 32767 - clamps at max
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 32767

        # Further scans stay clamped
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 32767


class TestTimerConditionTypeGuards:
    """Timer helper conditions remain BOOL-only for direct Tag inputs."""

    def test_on_delay_reset_rejects_int_tag(self):
        Enable = Bool("Enable")
        ResetValue = Int("ResetValue")

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="Non-BOOL tag"):
                    on_delay(Timer[1], preset=100).reset(ResetValue)


class TestTimerStructuralContract:
    """Timer instructions accept any UDT with Done: Bool and Acc: Int|Dint."""

    def test_custom_udt_with_int_acc_works(self, runner_factory):
        Enable = Bool("Enable")

        @udt()
        class MyTimer:
            Done: Bool  # noqa: F821
            Acc: Int  # noqa: F821

        with Program() as logic:
            with Rung(Enable):
                on_delay(MyTimer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["MyTimer_Acc"] == 10

    def test_custom_udt_with_dint_acc_works(self, runner_factory):
        Enable = Bool("Enable")

        @udt()
        class BigTimer:
            Done: Bool  # noqa: F821
            Acc: Dint  # noqa: F821

        with Program() as logic:
            with Rung(Enable):
                on_delay(BigTimer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["BigTimer_Acc"] == 10

    def test_rejects_non_bool_done(self):
        Enable = Bool("Enable")

        @udt()
        class BadDone:
            Done: Int  # noqa: F821
            Acc: Int  # noqa: F821

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="'Done' to be a Bool"):
                    on_delay(BadDone[1], preset=100)

    def test_rejects_non_int_acc(self):
        Enable = Bool("Enable")

        @udt()
        class BadAcc:
            Done: Bool  # noqa: F821
            Acc: Real  # noqa: F821

        with Program():
            with Rung(Enable):
                with pytest.raises(TypeError, match="'Acc' to be an Int or Dint"):
                    on_delay(BadAcc[1], preset=100)


class TestPositionalAndUnitAliases:
    """Test positional preset/unit args and human-friendly unit strings."""

    def test_on_delay_positional_preset(self, runner_factory):
        """on_delay(timer, 100) works as positional preset."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], 100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10

    def test_on_delay_positional_preset_and_unit(self, runner_factory):
        """on_delay(timer, 5, 's') works — 5 seconds in Ts unit."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], 5, "s")

        runner = runner_factory(logic, dt=1.0)
        runner.patch({"Enable": True})

        for _ in range(5):
            runner.step()

        assert runner.current_state.tags["Timer_Done"] is True
        assert runner.current_state.tags["Timer_Acc"] == 5

    def test_off_delay_positional_preset_and_unit(self, runner_factory):
        """off_delay(timer, 10, 'ms') works positionally."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                off_delay(Timer[1], 10, "ms")

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Done"] is True

    def test_on_delay_unit_alias_minutes(self, runner_factory):
        """on_delay with unit='min' normalizes to Tm."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=2, unit="min")

        runner = runner_factory(logic, dt=60.0)
        runner.patch({"Enable": True})

        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 1
        runner.step()
        assert runner.current_state.tags["Timer_Done"] is True

    def test_on_delay_unit_alias_hours(self, runner_factory):
        """on_delay with unit='h' normalizes to Th."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=1, unit="h")

        runner = runner_factory(logic, dt=3600.0)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Done"] is True

    def test_on_delay_unit_canonical_still_works(self, runner_factory):
        """Canonical unit strings like 'Tms' and 'Ts' still work."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], 100, "Tms")

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10

    def test_on_delay_keyword_preset_still_works(self, runner_factory):
        """Keyword preset= still works after making it positional."""
        Enable = Bool("Enable")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], preset=100)

        runner = runner_factory(logic, dt=0.010)
        runner.patch({"Enable": True})
        runner.step()
        assert runner.current_state.tags["Timer_Acc"] == 10

    def test_on_delay_positional_with_reset(self, runner_factory):
        """on_delay(timer, 100, 'ms').reset(tag) works."""
        Enable = Bool("Enable")
        ResetBtn = Bool("ResetBtn")

        with Program() as logic:
            with Rung(Enable):
                on_delay(Timer[1], 100, "ms").reset(ResetBtn)

        runner = runner_factory(logic, dt=0.050)
        runner.patch({"Enable": True, "ResetBtn": False})
        runner.run(cycles=3)
        assert runner.current_state.tags["Timer_Acc"] == 150
        assert runner.current_state.tags["Timer_Done"] is True

    def test_unknown_unit_raises(self):
        """Unknown unit string raises ValueError."""
        Enable = Bool("Enable")

        with Program():
            with Rung(Enable):
                with pytest.raises(ValueError, match="unknown time unit"):
                    on_delay(Timer[1], 100, "furlongs")

    def test_ambiguous_t_raises(self):
        """Bare 'T' raises ValueError for ambiguity."""
        Enable = Bool("Enable")

        with Program():
            with Rung(Enable):
                with pytest.raises(ValueError, match="ambiguous"):
                    on_delay(Timer[1], 100, "T")
