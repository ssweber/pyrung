"""Tests for rise() and fall() edge detection DSL functions.

These tests verify:
1. rise()/fall() create proper conditions
2. PLCRunner auto-tracks _prev:* values in memory
3. Edge detection works correctly across scan cycles
"""

from pyrung.core import Bit, PLCRunner, Program, Rung, latch, out, reset


class TestRiseDSL:
    """Test rise() DSL function for rising edge detection."""

    def test_rise_creates_rising_edge_condition(self):
        """rise() should create a RisingEdgeCondition."""
        from pyrung.core import rise
        from pyrung.core.condition import RisingEdgeCondition

        Button = Bit("Button")
        cond = rise(Button)

        assert isinstance(cond, RisingEdgeCondition)
        assert cond.tag is Button

    def test_rise_in_rung_fires_on_transition(self):
        """Rung with rise() should fire only on 0->1 transition."""
        from pyrung.core import rise

        Button = Bit("Button")
        Light = Bit("Light")

        with Program() as logic:
            with Rung(rise(Button)):
                out(Light)  # out() resets when rung false - shows one-shot behavior

        runner = PLCRunner(logic)

        # Initial state: Button=False, Light=False
        runner.patch({"Button": False, "Light": False})
        runner.step()
        assert runner.current_state.tags.get("Light") is False

        # Button still False - no edge
        runner.step()
        assert runner.current_state.tags.get("Light") is False

        # Button goes True - rising edge! Light turns on for ONE scan
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Button stays True - no edge, so out() resets Light to False
        runner.step()
        assert runner.current_state.tags.get("Light") is False  # Back to False!

    def test_rise_does_not_fire_when_already_on(self):
        """rise() should not fire if signal was already True."""
        from pyrung.core import rise

        Button = Bit("Button")
        Counter = Bit("Counter")  # Use bit as simple flag

        with Program() as logic:
            with Rung(rise(Button)):
                latch(Counter)

        runner = PLCRunner(logic)

        # Start with Button already True
        runner.patch({"Button": True, "Counter": False})
        runner.step()

        # No rising edge because prev was also True (initialized to False, then first scan)
        # Actually first scan: prev=False (default), current=True -> rising edge!
        assert runner.current_state.tags.get("Counter") is True

        # Now reset and continue - no more edges while Button stays True
        runner.patch({"Counter": False})
        runner.step()
        runner.step()  # Button still True, prev=True -> no edge
        assert runner.current_state.tags.get("Counter") is False

    def test_rise_fires_again_after_off_on_cycle(self):
        """rise() should fire again after signal goes off then on."""
        from pyrung.core import rise

        Button = Bit("Button")
        PulseCount = Bit("PulseCount")

        with Program() as logic:
            with Rung(rise(Button)):
                latch(PulseCount)

        runner = PLCRunner(logic)

        # First rising edge
        runner.patch({"Button": False, "PulseCount": False})
        runner.step()
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True

        # Reset the counter
        runner.patch({"PulseCount": False})
        runner.step()

        # Button goes off
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is False

        # Button goes on again - should fire!
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True


class TestFallDSL:
    """Test fall() DSL function for falling edge detection."""

    def test_fall_creates_falling_edge_condition(self):
        """fall() should create a FallingEdgeCondition."""
        from pyrung.core import fall
        from pyrung.core.condition import FallingEdgeCondition

        Button = Bit("Button")
        cond = fall(Button)

        assert isinstance(cond, FallingEdgeCondition)
        assert cond.tag is Button

    def test_fall_in_rung_fires_on_transition(self):
        """Rung with fall() should fire only on 1->0 transition."""
        from pyrung.core import fall

        Button = Bit("Button")
        Light = Bit("Light")

        with Program() as logic:
            with Rung(fall(Button)):
                out(Light)  # out() resets when rung false - shows one-shot behavior

        runner = PLCRunner(logic)

        # Initial state: Button=True, Light=False
        runner.patch({"Button": True, "Light": False})
        runner.step()
        # First scan: prev=False (default), current=True -> not falling edge
        assert runner.current_state.tags.get("Light") is False

        # Button stays True - no edge
        runner.step()
        assert runner.current_state.tags.get("Light") is False

        # Button goes False - falling edge! Light turns on for ONE scan
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Button stays False - no edge (already off), so out() resets Light
        runner.step()
        assert runner.current_state.tags.get("Light") is False  # Back to False!

    def test_fall_does_not_fire_when_already_off(self):
        """fall() should not fire if signal was already False."""
        from pyrung.core import fall

        Button = Bit("Button")
        Counter = Bit("Counter")

        with Program() as logic:
            with Rung(fall(Button)):
                latch(Counter)

        runner = PLCRunner(logic)

        # Start with Button already False
        runner.patch({"Button": False, "Counter": False})
        runner.step()
        # No falling edge: prev=False, current=False
        assert runner.current_state.tags.get("Counter") is False

        # Still False
        runner.step()
        assert runner.current_state.tags.get("Counter") is False

    def test_fall_fires_again_after_on_off_cycle(self):
        """fall() should fire again after signal goes on then off."""
        from pyrung.core import fall

        Button = Bit("Button")
        PulseCount = Bit("PulseCount")

        with Program() as logic:
            with Rung(fall(Button)):
                latch(PulseCount)

        runner = PLCRunner(logic)

        # Setup: Button on
        runner.patch({"Button": True, "PulseCount": False})
        runner.step()  # prev=False, current=True -> no fall
        runner.step()  # prev=True, current=True -> no fall

        # First falling edge
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True

        # Reset the counter
        runner.patch({"PulseCount": False})
        runner.step()

        # Button goes on
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is False

        # Button goes off again - should fire!
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("PulseCount") is True


class TestPrevValueTracking:
    """Test that PLCRunner properly tracks _prev:* values in memory."""

    def test_runner_updates_prev_values_after_scan(self):
        """Runner should update _prev:* in memory after each scan."""
        Button = Bit("Button")
        Light = Bit("Light")

        with Program() as logic:
            with Rung(Button):
                out(Light)

        runner = PLCRunner(logic)

        # Set initial value
        runner.patch({"Button": True})
        runner.step()

        # Check that _prev:Button was recorded
        assert runner.current_state.memory.get("_prev:Button") is True

        # Change Button
        runner.patch({"Button": False})
        runner.step()

        # _prev:Button should now be False (from previous scan)
        assert runner.current_state.memory.get("_prev:Button") is False

    def test_runner_tracks_multiple_tags(self):
        """Runner should track _prev:* for all tags that change."""
        A = Bit("A")
        B = Bit("B")
        Out = Bit("Out")

        with Program() as logic:
            with Rung(A, B):
                out(Out)

        runner = PLCRunner(logic)

        runner.patch({"A": True, "B": False})
        runner.step()

        assert runner.current_state.memory.get("_prev:A") is True
        assert runner.current_state.memory.get("_prev:B") is False

        runner.patch({"A": False, "B": True})
        runner.step()

        assert runner.current_state.memory.get("_prev:A") is False
        assert runner.current_state.memory.get("_prev:B") is True


class TestEdgeCombinations:
    """Test edge conditions combined with other conditions."""

    def test_rise_with_other_conditions(self):
        """rise() can be combined with other conditions in a rung."""
        from pyrung.core import rise

        Button = Bit("Button")
        Enable = Bit("Enable")
        Light = Bit("Light")

        with Program() as logic:
            with Rung(rise(Button), Enable):  # Both must be true
                latch(Light)

        runner = PLCRunner(logic)

        # Rising edge but Enable is False
        runner.patch({"Button": False, "Enable": False, "Light": False})
        runner.step()
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is False  # Enable was False

        # Reset Button
        runner.patch({"Button": False})
        runner.step()

        # Now Enable is True and Button rises
        runner.patch({"Enable": True})
        runner.step()
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

    def test_rise_and_fall_in_different_rungs(self):
        """rise() and fall() can control set/reset of same output."""
        from pyrung.core import fall, rise

        Button = Bit("Button")
        Light = Bit("Light")

        with Program() as logic:
            with Rung(rise(Button)):
                latch(Light)
            with Rung(fall(Button)):
                reset(Light)

        runner = PLCRunner(logic)

        runner.patch({"Button": False, "Light": False})
        runner.step()

        # Rising edge - Light on
        runner.patch({"Button": True})
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Stays on while Button held
        runner.step()
        assert runner.current_state.tags.get("Light") is True

        # Falling edge - Light off
        runner.patch({"Button": False})
        runner.step()
        assert runner.current_state.tags.get("Light") is False
